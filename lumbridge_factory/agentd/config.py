"""agentd's configuration: one JSON file, written by the host install."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Account:
    name: str
    tool: str  # "claude" | "codex"
    env_file: str = ""  # holds env_var; re-read on every turn so rotation just works
    env_var: str = ""  # the variable's name there, or in agentd's own environment
    child_var: str = ""  # its name in the child; default per tool
    codex_home: str = ""  # codex keeps its login in a directory, not a token


@dataclass(frozen=True)
class Tool:
    enabled: bool
    bin: str
    expected_version: str = ""


@dataclass(frozen=True)
class Config:
    socket: str
    state_dir: str
    worktrees_dir: str
    repos_dir: str
    agent_home: str
    repos: dict[str, str]  # "acme/widgets" -> clone URL
    tools: dict[str, Tool]
    accounts: dict[str, Account]
    socket_mode: int = 0o660
    socket_group: str = ""
    allowed_peer_uids: tuple[int, ...] = ()  # empty means only agentd's own uid
    child_env: dict[str, str] = field(default_factory=dict)  # GIT_SSH_COMMAND, git identity
    child_path: str = "/usr/local/bin:/usr/bin:/bin"
    push_prefixes: tuple[str, ...] = ("factory/",)
    deny_roots: tuple[str, ...] = ()
    max_turns: int = 8
    kill_grace_seconds: float = 10.0
    post_result_grace_seconds: float = 30.0
    extra_allowed_tools: tuple[str, ...] = ()
    settings_overrides: dict = field(default_factory=dict)



def _path(value: str) -> str:
    return os.path.abspath(os.path.expanduser(value))


def load(path: str | Path) -> Config:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"can't read {path}: {exc}") from exc
    return from_dict(raw)


def from_dict(raw: dict) -> Config:
    missing = [k for k in ("socket", "state_dir", "worktrees_dir", "repos_dir", "agent_home") if not raw.get(k)]
    if missing:
        raise ConfigError(f"missing: {', '.join(missing)}")
    tools_raw = raw.get("tools") or {}
    tools = {
        "claude": Tool(
            enabled=bool(tools_raw.get("claude", {}).get("enabled", True)),
            bin=tools_raw.get("claude", {}).get("bin", "claude"),
            expected_version=tools_raw.get("claude", {}).get("expected_version", ""),
        ),
        # Codex is built but stays off unless the config turns it on.
        "codex": Tool(
            enabled=bool(tools_raw.get("codex", {}).get("enabled", False)),
            bin=tools_raw.get("codex", {}).get("bin", "codex"),
            expected_version=tools_raw.get("codex", {}).get("expected_version", ""),
        ),
    }
    accounts = {}
    for name, item in (raw.get("accounts") or {}).items():
        tool = item.get("tool")
        if tool not in tools:
            raise ConfigError(f"account {name}: unknown tool {tool!r}")
        if "token" in item or "value" in item:
            raise ConfigError(f"account {name}: a token value doesn't belong in config; use env_file and env_var")
        accounts[name] = Account(
            name=name,
            tool=tool,
            env_file=_path(item["env_file"]) if item.get("env_file") else "",
            env_var=item.get("env_var", ""),
            child_var=item.get("child_var", ""),
            codex_home=_path(item["codex_home"]) if item.get("codex_home") else "",
        )
    mode = raw.get("socket_mode", "0660")
    return Config(
        socket=_path(raw["socket"]),
        state_dir=_path(raw["state_dir"]),
        worktrees_dir=_path(raw["worktrees_dir"]),
        repos_dir=_path(raw["repos_dir"]),
        agent_home=_path(raw["agent_home"]),
        repos=dict(raw.get("repos") or {}),
        tools=tools,
        accounts=accounts,
        socket_mode=int(mode, 8) if isinstance(mode, str) else int(mode),
        socket_group=raw.get("socket_group", ""),
        allowed_peer_uids=tuple(raw.get("allowed_peer_uids") or ()),
        child_env=dict(raw.get("child_env") or {}),
        child_path=raw.get("child_path", "/usr/local/bin:/usr/bin:/bin"),
        push_prefixes=tuple(raw.get("push_prefixes") or ("factory/",)),
        deny_roots=tuple(_path(p) for p in raw.get("deny_roots") or ()),
        max_turns=int(raw.get("max_turns", 8)),
        kill_grace_seconds=float(raw.get("kill_grace_seconds", 10.0)),
        post_result_grace_seconds=float(raw.get("post_result_grace_seconds", 30.0)),
        extra_allowed_tools=tuple(raw.get("extra_allowed_tools") or ()),
        settings_overrides=dict(raw.get("settings_overrides") or {}),
    )
