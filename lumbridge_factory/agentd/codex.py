"""Codex as an executor (ARCHITECTURE §6). Built, and off unless the config turns it on.

Never run against a real model yet. The argv and the
execpolicy are checked against the installed CLI; the event names come from its
`--json` docs and need a real turn before this is switched on.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from lumbridge_factory.core.executor import EventType, TurnOutcome

from .session_files import MCP_SERVER_NAME

_ACCOUNT_LIMIT = re.compile(r"usage limit|limit reached|quota|insufficient_quota|plan limit", re.I)
_RESUME = re.compile(r"no (?:rollout|session|thread|conversation) found|session .{0,40}not found|thread .{0,40}not found", re.I)
_TRANSIENT = re.compile(r"overloaded|\b(?:408|429|500|502|503|504)\b|connection (?:reset|error)|stream disconnected|timed out|rate.?limit", re.I)
_TEXT_LIMIT = 4000

# The same deny list as the Claude hook, as far as prefix rules can state it. Prefix
# rules can't say "only this branch", and they don't cover reads at all: the push
# limit rests on the git key and branch protection.
_FORBIDDEN_PREFIXES = [
    ["git", "push", "--force"],
    ["git", "push", "-f"],
    ["git", "push", "--force-with-lease"],
    ["git", "push", "--force-if-includes"],
    ["git", "push", "--tags"],
    ["git", "push", "--follow-tags"],
    ["git", "push", "--mirror"],
    ["git", "push", "--all"],
    ["git", "push", "--delete"],
    ["git", "push", "-d"],
    ["git", "push", "origin", "main"],
    ["git", "push", "origin", "HEAD:main"],
    ["git", "push", "origin", "HEAD:refs/heads/main"],
    ["git", "tag"],
    ["git", "config"],
    ["git", "remote", "add"],
    ["git", "remote", "set-url"],
    ["gh", "pr", "merge"],
    ["gh", "release"],
    ["gh", "auth"],
    ["tea", "pr", "merge"],
    ["tea", "pulls", "merge"],
    ["tea", "merge"],
    ["tea", "login"],
    ["env"],
    ["printenv"],
    ["sudo"],
    ["ssh"],
]


def exec_policy_text() -> str:
    lines = ["# Written by the factory agentd. Prefix rules only; see docs/AGENTD.md for the limits."]
    for prefix in _FORBIDDEN_PREFIXES:
        lines.append(
            f"prefix_rule(pattern={json.dumps(prefix)}, decision=\"forbidden\", "
            f"justification=\"factory policy: {' '.join(prefix)} is refused\")"
        )
    return "\n".join(lines) + "\n"


def install_exec_policy(codex_home: str) -> str:
    """Codex loads every `.rules` file in $CODEX_HOME/rules."""
    rules_dir = os.path.join(codex_home, "rules")
    os.makedirs(rules_dir, exist_ok=True)
    path = os.path.join(rules_dir, "factory.rules")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(exec_policy_text())
    return path


def _toml_string(value: str) -> str:
    return json.dumps(value)


def build_argv(
    bin_path: str,
    *,
    model: str,
    mcp_argv: list[str],
    role_text: str,
    last_message_file: str,
    git_common_dir: str,
    native_session_id: str | None,
) -> list[str]:
    config = [
        f"mcp_servers.{MCP_SERVER_NAME}.command={_toml_string(mcp_argv[0])}",
        f"mcp_servers.{MCP_SERVER_NAME}.args={json.dumps(mcp_argv[1:])}",
        f"developer_instructions={_toml_string(role_text)}",
        # A push needs the network, and a commit needs the worktree's gitdir, which sits
        # outside the workspace (the --add-dir trap found in an earlier factory).
        "sandbox_workspace_write.network_access=true",
        f"sandbox_workspace_write.writable_roots={json.dumps([git_common_dir])}",
        'sandbox_mode="workspace-write"',
        'approval_policy="never"',
    ]
    flags: list[str] = []
    for item in config:
        flags += ["-c", item]
    flags += ["-m", model, "--json", "-o", last_message_file]
    if native_session_id:
        return [bin_path, "exec", "resume", *flags, native_session_id, "-"]
    return [bin_path, "exec", *flags, "-"]


@dataclass
class Parser:
    native_session_id: str | None = None
    tokens: int = 0
    final: dict | None = None
    failure: str = ""
    _announced: bool = False

    def feed(self, obj: dict) -> list[tuple[EventType, dict]]:
        events: list[tuple[EventType, dict]] = []
        kind = obj.get("type")
        if kind == "thread.started":
            tid = obj.get("thread_id")
            if isinstance(tid, str) and tid:
                self.native_session_id = tid
                if not self._announced:
                    self._announced = True
                    events.append((EventType.STARTED, {"native_session_id": tid}))
        elif kind == "item.completed":
            item = obj.get("item") or {}
            item_type = item.get("type")
            if item_type == "agent_message" and item.get("text"):
                events.append((EventType.TEXT, {"text": str(item["text"])[:_TEXT_LIMIT]}))
            elif item_type in ("command_execution", "mcp_tool_call", "file_change", "web_search"):
                events.append((EventType.TOOL, {"name": str(item.get("tool") or item_type)}))
        elif kind == "turn.completed":
            self.final = obj
            usage = obj.get("usage") or {}
            count = sum(usage[k] for k in ("input_tokens", "output_tokens") if isinstance(usage.get(k), int))
            self.tokens += count
            events.append((EventType.USAGE, {"tokens": self.tokens}))
        elif kind == "turn.failed":
            error = obj.get("error") or {}
            self.failure = str(error.get("message") or "the turn failed")
        elif kind == "error":
            self.failure = str(obj.get("message") or "error")
        return events

    @property
    def done(self) -> bool:
        return self.final is not None

    def classify(self, exit_code: int | None, stderr_tail: str, resumed: bool) -> tuple[TurnOutcome, str]:
        if self.final is not None and not self.failure:
            return TurnOutcome.COMPLETED, ""
        if exit_code is not None and exit_code < 0 and self.final is None:
            return TurnOutcome.INTERRUPTED, f"the CLI was killed by signal {-exit_code}"
        text = "\n".join(p for p in (self.failure, stderr_tail) if p).strip()
        tail = text[-400:]
        if resumed and _RESUME.search(text):
            return TurnOutcome.RESUME_FAILED, tail
        if _ACCOUNT_LIMIT.search(text):
            return TurnOutcome.ACCOUNT_LIMIT, tail
        if _TRANSIENT.search(text):
            return TurnOutcome.TRANSIENT, tail
        return TurnOutcome.FAILED, tail or f"the CLI exited with status {exit_code}"
