"""Claude Code as an executor (ARCHITECTURE §6): one `claude -p` process per turn."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lumbridge_factory.core.executor import EventType, TurnOutcome

from .session_files import MCP_SERVER_NAME

_ACCOUNT_LIMIT = re.compile(
    r"usage limit|limit reached|hit your .{0,20}limit|out of extra usage|session limit|credit balance|quota exceeded",
    re.I,
)
_RESUME = re.compile(r"No conversation found with session ID", re.I)
_TRANSIENT = re.compile(
    r"overloaded|ECONNRESET|ETIMEDOUT|ECONNREFUSED|EAI_AGAIN|connection (?:reset|error|closed)|socket hang up|fetch failed"
    r"|network error|API Error: (?:408|429|5\d\d)|rate.?limit|internal server error|service unavailable|bad gateway|gateway timeout",
    re.I,
)
_TEXT_LIMIT = 4000


def build_argv(
    bin_path: str,
    *,
    model: str,
    settings: str,
    mcp_config: str,
    role_file: str,
    native_session_id: str | None,
) -> list[str]:
    argv = [
        bin_path,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model,
        "--permission-mode",
        "dontAsk",
        # Nobody is there to answer a prompt.
        "--permission-prompts",
        "none",
        # Only this ticket's settings. Project or local settings in the worktree could
        # otherwise turn the hook off with `disableAllHooks`.
        "--setting-sources",
        "",
        "--settings",
        settings,
        "--strict-mcp-config",
        f"--mcp-config={mcp_config}",
        "--append-system-prompt-file",
        role_file,
    ]
    if native_session_id:
        argv += ["--resume", native_session_id]
    return argv


@dataclass
class Parser:
    """Turns stream-json lines into events. Text stays opaque; only shape is read."""

    native_session_id: str | None = None
    tokens: int = 0
    final: dict | None = None
    _message_tokens: dict = field(default_factory=dict)
    _announced: bool = False

    def feed(self, obj: dict) -> list[tuple[EventType, dict]]:
        events: list[tuple[EventType, dict]] = []
        kind = obj.get("type")
        sid = obj.get("session_id")
        if isinstance(sid, str) and sid:
            self.native_session_id = sid
            if not self._announced:
                self._announced = True
                events.append((EventType.STARTED, {"native_session_id": sid}))
        if kind == "assistant":
            message = obj.get("message") or {}
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    events.append((EventType.TEXT, {"text": str(block["text"])[:_TEXT_LIMIT]}))
                elif block.get("type") == "tool_use":
                    events.append((EventType.TOOL, {"name": str(block.get("name") or "")}))
            usage = message.get("usage")
            mid = message.get("id")
            if isinstance(usage, dict) and mid:
                self._message_tokens[mid] = _count(usage)
                self.tokens = sum(self._message_tokens.values())
                events.append((EventType.USAGE, {"tokens": self.tokens}))
        elif kind == "result":
            self.final = obj
            usage = obj.get("usage")
            if isinstance(usage, dict):
                self.tokens = _count(usage)
                events.append((EventType.USAGE, {"tokens": self.tokens}))
        return events

    @property
    def done(self) -> bool:
        return self.final is not None

    def classify(self, exit_code: int | None, stderr_tail: str, resumed: bool) -> tuple[TurnOutcome, str]:
        """The outcome from the CLI's own evidence. If unsure, `failed`."""
        final = self.final
        if final is not None and final.get("subtype") == "success" and not final.get("is_error"):
            return TurnOutcome.COMPLETED, ""
        pieces = [stderr_tail]
        status = None
        if final is not None:
            status = final.get("api_error_status")
            pieces.append(str(final.get("result") or ""))
            errors = final.get("errors")
            if isinstance(errors, list):
                pieces.extend(str(e) for e in errors)
        text = "\n".join(p for p in pieces if p).strip()
        tail = text[-400:]
        if exit_code is not None and exit_code < 0 and final is None:
            return TurnOutcome.INTERRUPTED, f"the CLI was killed by signal {-exit_code}"
        if resumed and _RESUME.search(text):
            return TurnOutcome.RESUME_FAILED, tail
        if _ACCOUNT_LIMIT.search(text):
            return TurnOutcome.ACCOUNT_LIMIT, tail
        if isinstance(status, int) and (status in (408, 429) or 500 <= status < 600):
            return TurnOutcome.TRANSIENT, tail
        if _TRANSIENT.search(text):
            return TurnOutcome.TRANSIENT, tail
        if final is None and not text:
            tail = f"the CLI exited with status {exit_code} and no result"
        return TurnOutcome.FAILED, tail or f"the CLI exited with status {exit_code}"


def _count(usage: dict) -> int:
    # Cache reads are excluded: they'd dwarf the rest, and a budget should measure work.
    total = 0
    for key in ("input_tokens", "cache_creation_input_tokens", "output_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            total += value
    return total


__all__ = ["build_argv", "Parser", "MCP_SERVER_NAME"]
