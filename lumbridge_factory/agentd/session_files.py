"""Files written for an Assignment and its turns: outside the worktree, never a token.

Per session: policy.json, settings.json, role.md. Per turn: mcp.json, because it names
that turn's inbox.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

from lumbridge_factory.core.executor import PromptParts, Role, StartRequest

from .config import Config
from .worktrees import Prepared

# Not resolved: through the `current` symlink, a session that outlives a release gets the new hook, and removing the old release
# can't leave it calling a missing one, which exits 127 and doesn't block.
HOOK_PATH = Path(__file__).absolute().with_name("hook.py")
MCP_PATH = Path(__file__).absolute().with_name("mcp_tools.py")
MCP_SERVER_NAME = "factory-tools"
BUILDER_TOOLS = ("Bash", "Read", "Edit", "Write", "NotebookEdit", "Glob", "Grep", "TodoWrite", "Task", "Agent")
REVIEWER_TOOLS = ("Bash", "Read", "Glob", "Grep", "TodoWrite", "Task", "Agent")


def write_private(path: str, text: str) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def homes(config: Config) -> list[str]:
    """Both the agent's HOME and the login's real home, when they differ."""
    out = [config.agent_home]
    try:
        import pwd

        real = pwd.getpwuid(os.geteuid()).pw_dir
        if real and real not in out:
            out.append(real)
    except KeyError:
        pass
    return out


def policy_dict(config: Config, request: StartRequest, prepared: Prepared) -> dict:
    deny_roots = [*config.deny_roots, os.path.dirname(config.socket)]
    return {
        "worktree": os.path.realpath(prepared.path),
        "worktrees_root": os.path.realpath(config.worktrees_dir),
        "homes": homes(config),
        "state_dir": os.path.realpath(config.state_dir),
        "common_dir": os.path.realpath(prepared.common_dir),
        "push_branches": [prepared.branch] if prepared.branch else [],
        "push_prefixes": list(config.push_prefixes),
        "read_only": bool(request.worktree.read_only or request.role == Role.REVIEWER),
        "deny_roots": deny_roots,
        "base": request.worktree.base,
    }


def settings_dict(config: Config, request: StartRequest, policy_path: str, audit_path: str) -> dict:
    tools = REVIEWER_TOOLS if request.role == Role.REVIEWER else BUILDER_TOOLS
    hook = shlex.join([sys.executable, "-I", str(HOOK_PATH), "--policy", policy_path, "--audit", audit_path])
    settings = {
        "permissions": {
            "allow": [*tools, *config.extra_allowed_tools, f"mcp__{MCP_SERVER_NAME}"],
            "defaultMode": "dontAsk",
        },
        "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": hook, "timeout": 30}]}]},
        "disableAllHooks": False,
        "autoMemoryEnabled": False,
        # A ticket can resume long after it ended (§5.8); the default sweep is 30 days.
        "cleanupPeriodDays": 3650,
    }
    settings.update(config.settings_overrides)
    return settings


def role_text(parts: PromptParts, request: StartRequest, prepared: Prepared) -> str:
    sections = [parts.system.strip()]
    if parts.rule_instructions.strip():
        sections.append(parts.rule_instructions.strip())
    where = f"Your worktree is {prepared.path}."
    if prepared.branch:
        where += f" Your branch is {prepared.branch}; push only to it, as `git push origin HEAD:{prepared.branch}`."
    sections.append(
        "## Your environment\n\n"
        f"{where}\n"
        f"Report through the `report` tool of the `{MCP_SERVER_NAME}` server. It's the only way the factory learns your "
        "state, so end every turn with one. Read this repository's AGENTS.md or CLAUDE.md, if it has one, before you "
        "change anything: this session doesn't load them for you.\n"
        "A policy hook refuses some commands. When it does, don't look for a way around it: use `report` with status "
        "`blocked` and say what you needed. The one exception is a refused background task or wakeup: run the same "
        "thing in the foreground instead."
    )
    return "\n\n".join(sections) + "\n"


def write_session_files(config: Config, request: StartRequest, prepared: Prepared, session_dir: str) -> dict[str, str]:
    policy_path = os.path.join(session_dir, "policy.json")
    settings_path = os.path.join(session_dir, "settings.json")
    audit_path = os.path.join(session_dir, "hook-denials.jsonl")
    role_path = os.path.join(session_dir, "role.md")
    write_private(policy_path, json.dumps(policy_dict(config, request, prepared), indent=2))
    write_private(settings_path, json.dumps(settings_dict(config, request, policy_path, audit_path), indent=2))
    write_private(role_path, role_text(request.prompt_parts, request, prepared))
    return {"policy": policy_path, "settings": settings_path, "role": role_path, "audit": audit_path}


def mcp_argv(turn_dir: str, role: str) -> list[str]:
    return [sys.executable, "-I", str(MCP_PATH), "--turn-dir", turn_dir, "--role", role]


def write_mcp_config(turn_dir: str, role: str) -> str:
    argv = mcp_argv(turn_dir, role)
    path = os.path.join(turn_dir, "mcp.json")
    write_private(path, json.dumps({"mcpServers": {MCP_SERVER_NAME: {"type": "stdio", "command": argv[0], "args": argv[1:]}}}, indent=2))
    return path
