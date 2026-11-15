#!/usr/bin/env python3
"""factory-tools: the stdio MCP server through which a worker reports (ARCHITECTURE §6).

The agent CLI starts one per turn. It can only append to its turn's inbox file and
wait for a recall answer; it holds no secrets and talks to nothing else. Everything
it writes is a claim that factoryd re-checks.

Newline-delimited JSON-RPC 2.0 on stdio. Runs under `python3 -I`, so it stays
self-contained: the status and verdict names below are repeated from
lumbridge_factory/core/executor.py, and a test keeps the two in step.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
import uuid

SERVER_NAME = "factory-tools"
SERVER_VERSION = "1"
DEFAULT_PROTOCOL = "2025-06-18"
STATUSES = ("progress", "waiting", "ready", "blocked", "failed", "dropped")
VERDICTS = ("pass", "changes_requested")
REPLY_PURPOSES = ("ack", "answer")
# PROTOCOL.md: a reviewer's only output is its report.
REVIEWER_TOOLS = ("report", "recall")
MAX_TEXT = 8000
RECALL_TIMEOUT_SECONDS = 60.0


class ToolError(Exception):
    """Goes back to the model as an error it can correct."""


def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


TOOLS = [
    {
        "name": "report",
        "description": (
            "Report your status to the factory. This is the only way it learns your state; free text is never read for "
            "state. End every turn with a report. Use ready when your work is pushed: the factory opens the PR, so give pr only "
            "if you were told its number. Use waiting or blocked with at most one concrete question, failed or dropped with "
            "the reason in summary, progress for an update."
        ),
        "inputSchema": _schema(
            {
                "status": {"type": "string", "enum": list(STATUSES)},
                "summary": {"type": "string", "description": "A short plain summary."},
                "pr": {"type": "integer", "description": "The pull request number, if there is one."},
                "question": {"type": "string", "description": "One concrete question, with status waiting or blocked."},
                "verdict": {"type": "string", "enum": list(VERDICTS), "description": "Reviewers only, with status ready."},
                "findings": {"type": "string", "description": "Reviewers only: what must change, and where."},
            },
            ["status", "summary"],
        ),
    },
    {
        "name": "reply",
        "description": "Queue an acknowledgement or an answer on this ticket's own thread. The factory posts it.",
        "inputSchema": _schema(
            {"purpose": {"type": "string", "enum": list(REPLY_PURPOSES)}, "text": {"type": "string"}},
            ["purpose", "text"],
        ),
    },
    {
        "name": "follow",
        "description": "Subscribe this ticket to a thread, for context only.",
        "inputSchema": _schema({"thread": {"type": "string"}}, ["thread"]),
    },
    {
        "name": "unrelated",
        "description": "Drop a thread that was attached to this ticket by mistake.",
        "inputSchema": _schema({"thread": {"type": "string"}}, ["thread"]),
    },
    {
        "name": "handback",
        "description": "Give a follow-up back to the router because it doesn't fit this ticket's scope.",
        "inputSchema": _schema({"trigger": {"type": "string"}, "reason": {"type": "string"}}, ["trigger", "reason"]),
    },
    {
        "name": "recall",
        "description": "Read a trigger, thread, ticket or earlier report in full, by its id.",
        "inputSchema": _schema({"id": {"type": "string"}}, ["id"]),
    },
]


class Server:
    def __init__(self, turn_dir: str, role: str, recall_timeout: float = RECALL_TIMEOUT_SECONDS) -> None:
        self.turn_dir = turn_dir
        self.role = role
        self.recall_timeout = recall_timeout

    # --- inbox ---------------------------------------------------------------

    def _append(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        path = os.path.join(self.turn_dir, "inbox.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    # --- tools ---------------------------------------------------------------

    @staticmethod
    def _text(args: dict, key: str, required: bool = True) -> str:
        value = args.get(key)
        if value is None:
            if required:
                raise ToolError(f"{key} is required")
            return ""
        if not isinstance(value, str):
            raise ToolError(f"{key} must be a string")
        if required and not value.strip():
            raise ToolError(f"{key} must not be empty")
        if len(value) > MAX_TEXT:
            raise ToolError(f"{key} is too long: {len(value)} characters, the limit is {MAX_TEXT}")
        return value

    def tool_report(self, args: dict) -> str:
        status = args.get("status")
        if status not in STATUSES:
            raise ToolError(f"status must be one of {', '.join(STATUSES)}")
        report: dict = {"status": status, "summary": self._text(args, "summary")}
        pr = args.get("pr")
        if pr is not None:
            if isinstance(pr, bool) or not isinstance(pr, int) or pr <= 0:
                raise ToolError("pr must be a positive integer")
            report["pr"] = pr
        question = args.get("question")
        if question is not None:
            if not isinstance(question, str) or not question.strip():
                raise ToolError("question must be one non-empty string")
            if status not in ("waiting", "blocked"):
                raise ToolError("question needs status waiting or blocked")
            report["question"] = self._text(args, "question")
        verdict = args.get("verdict")
        findings = self._text(args, "findings", required=False)
        if self.role == "reviewer":
            if status == "ready" and verdict not in VERDICTS:
                raise ToolError(f"a reviewer's ready report needs verdict: {' or '.join(VERDICTS)}")
            if verdict is not None and verdict not in VERDICTS:
                raise ToolError(f"verdict must be {' or '.join(VERDICTS)}")
            if verdict == "changes_requested" and not findings.strip():
                raise ToolError("changes_requested needs findings")
        elif verdict is not None or findings:
            raise ToolError("verdict and findings are for reviewers")
        if verdict is not None:
            report["verdict"] = verdict
        if findings:
            report["findings"] = findings
        self._append({"kind": "report", "report": report})
        return f"Recorded: {status}."

    def tool_reply(self, args: dict) -> str:
        purpose = args.get("purpose")
        if purpose not in REPLY_PURPOSES:
            raise ToolError(f"purpose must be one of {', '.join(REPLY_PURPOSES)}")
        self._append({"kind": "reply", "purpose": purpose, "text": self._text(args, "text")})
        return "Queued."

    def tool_follow(self, args: dict) -> str:
        self._append({"kind": "follow", "thread": self._text(args, "thread")})
        return "Following."

    def tool_unrelated(self, args: dict) -> str:
        self._append({"kind": "unrelated", "thread": self._text(args, "thread")})
        return "Dropped."

    def tool_handback(self, args: dict) -> str:
        self._append({"kind": "handback", "trigger": self._text(args, "trigger"), "text": self._text(args, "reason")})
        return "Handed back."

    def tool_recall(self, args: dict) -> str:
        object_id = self._text(args, "id")
        request_id = uuid.uuid4().hex
        answer = os.path.join(self.turn_dir, "recall", request_id)
        self._append({"kind": "recall", "request_id": request_id, "object_id": object_id})
        deadline = time.monotonic() + self.recall_timeout
        while time.monotonic() < deadline:
            try:
                with open(answer, encoding="utf-8") as fh:
                    return fh.read()
            except FileNotFoundError:
                time.sleep(0.05)
        raise ToolError(f"recall of {object_id} timed out; carry on without it or try again")

    # --- JSON-RPC ------------------------------------------------------------

    def handle(self, msg: dict) -> dict | None:
        method = msg.get("method")
        msg_id = msg.get("id")
        if not isinstance(method, str):
            if msg_id is None:
                return None
            return self._error(msg_id, -32600, "invalid request")
        if msg_id is None:
            return None  # a notification
        if method == "initialize":
            params = msg.get("params") or {}
            version = params.get("protocolVersion") if isinstance(params, dict) else None
            return self._ok(
                msg_id,
                {
                    "protocolVersion": version if isinstance(version, str) else DEFAULT_PROTOCOL,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            )
        if method == "ping":
            return self._ok(msg_id, {})
        if method == "tools/list":
            return self._ok(msg_id, {"tools": [t for t in TOOLS if self._offered(t["name"])]})
        if method == "tools/call":
            return self._call(msg_id, msg.get("params") or {})
        return self._error(msg_id, -32601, f"unknown method {method}")

    def _offered(self, name: str) -> bool:
        return self.role != "reviewer" or name in REVIEWER_TOOLS

    def _call(self, msg_id, params: dict) -> dict:
        name = params.get("name") if isinstance(params, dict) else None
        known = isinstance(name, str) and re.fullmatch(r"[a-z]+", name) and self._offered(name)
        func = getattr(self, f"tool_{name}", None) if known else None
        if func is None:
            return self._error(msg_id, -32602, f"unknown tool {name}")
        args = params.get("arguments") or {}
        try:
            if not isinstance(args, dict):
                raise ToolError("arguments must be an object")
            return self._ok(msg_id, {"content": [{"type": "text", "text": func(args)}]})
        except ToolError as exc:
            return self._ok(msg_id, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
        except OSError as exc:
            return self._ok(msg_id, {"content": [{"type": "text", "text": f"inbox unavailable: {exc.strerror}"}], "isError": True})

    @staticmethod
    def _ok(msg_id, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error(msg_id, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    def serve(self, stdin, stdout) -> None:
        for line in stdin:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
                reply = self.handle(msg) if isinstance(msg, dict) else self._error(None, -32600, "invalid request")
            except ValueError:
                reply = self._error(None, -32700, "parse error")
            if reply is not None:
                stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                stdout.flush()


def main(argv: list[str]) -> int:
    # It needs no credentials, whatever the parent left in the environment.
    for name in list(os.environ):
        if "TOKEN" in name or name.endswith("_API_KEY"):
            del os.environ[name]
    turn_dir = role = None
    for i, arg in enumerate(argv):
        if arg == "--turn-dir":
            turn_dir = argv[i + 1]
        elif arg == "--role":
            role = argv[i + 1]
    if not turn_dir or role not in ("builder", "reviewer"):
        print("usage: mcp_tools.py --turn-dir DIR --role builder|reviewer", file=sys.stderr)
        return 2
    Server(turn_dir, role).serve(sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
