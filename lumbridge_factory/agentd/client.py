"""factoryd's side of the socket: core.executor.Executor over docs/PROTOCOL.md.

Stdlib only, and safe to import in factoryd: it pulls in nothing from agentd's process code.
"""

from __future__ import annotations

import json
import socket
from typing import Iterator

from lumbridge_factory.core.executor import (
    PROTOCOL_VERSION,
    ExecutorError,
    RunningTurn,
    StartRequest,
    TurnRequest,
    TurnResult,
    TurnStreamItem,
    event_from_wire,
    result_from_wire,
    to_wire,
)


class AgentdClient:
    def __init__(self, socket_path: str, timeout: float = 30.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def _connect(self, op: str, args: dict, timeout: float | None) -> tuple[socket.socket, object]:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.connect(self.socket_path)
            payload = json.dumps({"v": PROTOCOL_VERSION, "op": op, "args": args}, ensure_ascii=False)
            sock.sendall((payload + "\n").encode())
            return sock, sock.makefile("rb")
        except OSError as exc:
            sock.close()
            raise ExecutorError("unavailable", f"agentd isn't reachable: {exc.strerror or exc}") from exc

    @staticmethod
    def _next(reader) -> dict:
        try:
            line = reader.readline()
        except OSError as exc:
            raise ExecutorError("unavailable", f"agentd connection failed: {exc}") from exc
        if not line:
            raise ExecutorError("unavailable", "agentd closed the connection")
        try:
            return json.loads(line)
        except ValueError as exc:
            raise ExecutorError("internal", "agentd sent something that isn't JSON", retryable=False) from exc

    @staticmethod
    def _unwrap(msg: dict):
        if msg.get("ok"):
            return msg.get("result")
        error = msg.get("error") or {}
        raise ExecutorError(error.get("code", "internal"), error.get("message", ""), bool(error.get("retryable")))

    def _call(self, op: str, args: dict | None = None, timeout: float | None = -1):
        wait = self.timeout if timeout == -1 else timeout
        sock, reader = self._connect(op, args or {}, wait)
        try:
            return self._unwrap(self._next(reader))
        finally:
            reader.close()
            sock.close()

    def hello(self) -> dict:
        return self._call("hello")

    def start(self, request: StartRequest) -> str:
        # Preparing a worktree can mean a first clone, so it gets no short timeout.
        return self._call("start", to_wire(request), timeout=None)["session_id"]

    def turn(self, request: TurnRequest) -> Iterator[TurnStreamItem]:
        """Dropping this iterator doesn't stop the turn. A second call with the same turn_id attaches."""
        sock, reader = self._connect("turn", json.loads(json.dumps(to_wire(request))), None)
        try:
            while True:
                msg = self._next(reader)
                if "event" in msg:
                    yield event_from_wire(msg["event"])
                else:
                    yield result_from_wire(self._unwrap(msg))
                    return
        finally:
            reader.close()
            sock.close()

    def stop(self, session_id: str) -> None:
        self._call("stop", {"session_id": session_id})

    def running(self) -> list[RunningTurn]:
        return [RunningTurn(t["turn_id"], t["session_id"]) for t in self._call("running")]

    def collect(self, turn_id: str) -> TurnResult | None:
        result = self._call("collect", {"turn_id": turn_id})
        return None if result is None else result_from_wire(result)

    def forget(self, turn_id: str) -> None:
        self._call("forget", {"turn_id": turn_id})

    def answer_recall(self, session_id: str, request_id: str, text: str) -> None:
        self._call("answer_recall", {"session_id": session_id, "request_id": request_id, "text": text})

    def remove_session(self, session_id: str, worktree: bool) -> None:
        # Deleting a big worktree can outlast the usual timeout.
        self._call("remove_session", {"session_id": session_id, "worktree": worktree}, timeout=None)
