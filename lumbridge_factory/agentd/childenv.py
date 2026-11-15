"""The environment of an agent child: cleared, then a short allowlist (ARCHITECTURE §12).

Nothing is inherited from agentd. A token exists only in this dict, which goes to
Popen and is never written anywhere.
"""

from __future__ import annotations

import os
import pwd
import re

from pathlib import Path

from .config import Account, Config
from .procs import TURN_VAR

SCRUB_SHELL = Path(__file__).absolute().with_name("scrub_shell.sh")

# Names a config may not smuggle into a child through child_env.
_FORBIDDEN_CHILD_ENV = re.compile(r"TOKEN|API_KEY|SECRET|PASSWORD|CREDENTIAL|^LD_|^CLAUDE_CONFIG_DIR$|^HOME$|^PATH$", re.I)
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_DEFAULT_CHILD_VAR = {"claude": "CLAUDE_CODE_OAUTH_TOKEN", "codex": "OPENAI_API_KEY"}


class TokenError(Exception):
    """Says which account and what went wrong. Never carries a value."""


def read_env_file(path: str, name: str) -> str | None:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            m = _LINE.match(line)
            if m and m.group(1) == name:
                value = m.group(2).strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                return value
    return None


def account_token(account: Account) -> str | None:
    """The account's token, or None for a login that lives in a directory (codex)."""
    if not account.env_var:
        if account.tool == "codex" and account.codex_home:
            return None
        raise TokenError(f"account {account.name} has no env_var")
    if account.env_file:
        try:
            value = read_env_file(account.env_file, account.env_var)
        except OSError as exc:
            raise TokenError(f"account {account.name}: can't read its env file ({exc.strerror})") from None
    else:
        value = os.environ.get(account.env_var)
    if not value:
        raise TokenError(f"account {account.name}: {account.env_var} is not set")
    return value


def base_env(config: Config, marker: str = "") -> dict[str, str]:
    try:
        user = pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        user = "factory"
    env = {
        "PATH": config.child_path,
        "HOME": config.agent_home,
        "USER": user,
        "LOGNAME": user,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "dumb",
        "SHELL": "/bin/bash",
        "TMPDIR": os.path.join(config.agent_home, "tmp"),
        "GIT_TERMINAL_PROMPT": "0",
    }
    for key, value in config.child_env.items():
        if _FORBIDDEN_CHILD_ENV.search(key):
            raise TokenError(f"child_env may not set {key}")
        env[key] = value
    if marker:
        env[TURN_VAR] = marker
    return env


def turn_env(config: Config, account: Account, marker: str) -> tuple[dict[str, str], list[str]]:
    """Returns (env, values to redact from the turn's output)."""
    env = base_env(config, marker)
    token = account_token(account)
    secrets: list[str] = []
    if account.tool == "claude":
        env.update(
            {
                # Claude Code keeps the token; what its tools run doesn't get it.
                "CLAUDE_CODE_SHELL_PREFIX": str(SCRUB_SHELL),
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_AUTOUPDATER": "1",
                "DISABLE_TELEMETRY": "1",
                "DISABLE_ERROR_REPORTING": "1",
                # Background tasks die with the turn (the hook refuses them), so a long suite runs in the
                # foreground: up to 30 minutes, inside the 45-minute turn.
                "BASH_MAX_TIMEOUT_MS": config.child_env.get("BASH_MAX_TIMEOUT_MS", "1800000"),
            }
        )
    else:
        env["CODEX_HOME"] = account.codex_home or os.path.join(config.agent_home, ".codex")
    if token is not None:
        env[account.child_var or _DEFAULT_CHILD_VAR[account.tool]] = token
        secrets.append(token)
    return env, secrets


def redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret and secret in text:
            text = text.replace(secret, "[redacted]")
    return text
