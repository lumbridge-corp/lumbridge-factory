"""agentd's socket server: docs/PROTOCOL.md on a unix socket. One request per connection."""

from __future__ import annotations

import grp
import json
import logging
import os
import signal
import socket
import socketserver
import struct
import threading

from lumbridge_factory.core.executor import (
    PROTOCOL_VERSION,
    ExecutorError,
    TurnEvent,
    start_request_from_wire,
    to_wire,
    turn_request_from_wire,
)

from . import procs
from .config import Config
from .turns import TurnManager

log = logging.getLogger("lumbridge_factory.agentd")
MAX_REQUEST_BYTES = 16 * 1024 * 1024


def _send(wfile, payload: dict) -> None:
    wfile.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
    wfile.flush()


def _error(code: str, message: str = "", retryable: bool = False) -> dict:
    return {"ok": False, "error": {"code": code, "message": message, "retryable": retryable}}


class Handler(socketserver.StreamRequestHandler):
    server: "Server"

    def handle(self) -> None:
        try:
            if not self.server.peer_allowed(self.request):
                _send(self.wfile, _error("bad_request", "this peer may not use agentd"))
                return
            line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if not line:
                return
            if len(line) > MAX_REQUEST_BYTES:
                _send(self.wfile, _error("bad_request", "request too large"))
                return
            self._dispatch(line)
        except (BrokenPipeError, ConnectionResetError):
            pass  # a dropped client doesn't stop a turn

    def _dispatch(self, line: bytes) -> None:
        try:
            msg = json.loads(line)
            if not isinstance(msg, dict):
                raise ValueError("not an object")
        except ValueError:
            _send(self.wfile, _error("bad_request", "not valid JSON"))
            return
        if msg.get("v") != PROTOCOL_VERSION:
            _send(self.wfile, _error("protocol_mismatch", f"this agentd speaks version {PROTOCOL_VERSION}"))
            return
        op = msg.get("op")
        args = msg.get("args") or {}
        func = getattr(self, f"op_{op}", None) if isinstance(op, str) and op.isidentifier() else None
        if func is None:
            _send(self.wfile, _error("bad_request", f"unknown op {op!r}"))
            return
        try:
            func(args)
        except ExecutorError as exc:
            _send(self.wfile, _error(exc.code, exc.message, exc.retryable))
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            _send(self.wfile, _error("bad_request", f"bad arguments for {op}: {type(exc).__name__}: {exc}"))
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception:  # noqa: BLE001 - the daemon outlives one bad request
            log.exception("op %s failed", op)
            _send(self.wfile, _error("internal", f"{op} failed; see agentd's log", True))

    def _ok(self, result) -> None:
        _send(self.wfile, {"ok": True, "result": result})

    @property
    def turns(self) -> TurnManager:
        return self.server.manager

    def op_hello(self, args: dict) -> None:
        tools = [n for n, t in self.server.config.tools.items() if t.enabled and n not in self.turns.tool_problems]
        self._ok({"protocol": PROTOCOL_VERSION, "tools": tools})

    def op_start(self, args: dict) -> None:
        self._ok({"session_id": self.turns.start_session(start_request_from_wire(args))})

    def op_turn(self, args: dict) -> None:
        for item in self.turns.turn(turn_request_from_wire(args)):
            if isinstance(item, TurnEvent):
                _send(self.wfile, {"event": to_wire(item)})
            else:
                self._ok(to_wire(item))

    def op_stop(self, args: dict) -> None:
        self.turns.stop(args["session_id"])
        self._ok({})

    def op_running(self, args: dict) -> None:
        self._ok([to_wire(t) for t in self.turns.running()])

    def op_collect(self, args: dict) -> None:
        result = self.turns.collect(args["turn_id"])
        self._ok(None if result is None else to_wire(result))

    def op_forget(self, args: dict) -> None:
        self.turns.forget(args["turn_id"])
        self._ok({})

    def op_remove_session(self, args: dict) -> None:
        self.turns.remove_session(args["session_id"], bool(args.get("worktree")))
        self._ok({})

    def op_answer_recall(self, args: dict) -> None:
        self.turns.answer_recall(args["session_id"], args["request_id"], args["text"])
        self._ok({})


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 64

    def __init__(self, config: Config, manager: TurnManager) -> None:
        self.config = config
        self.manager = manager
        allowed = set(config.allowed_peer_uids) or {os.geteuid()}
        self.allowed_uids = allowed
        _clear_stale_socket(config.socket)
        os.makedirs(os.path.dirname(config.socket), exist_ok=True)
        previous = os.umask(0o177)  # never a moment when the socket is open to anyone
        try:
            super().__init__(config.socket, Handler)
        finally:
            os.umask(previous)
        if config.socket_group:
            os.chown(config.socket, -1, grp.getgrnam(config.socket_group).gr_gid)
        os.chmod(config.socket, config.socket_mode)

    def peer_allowed(self, sock: socket.socket) -> bool:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
        return uid in self.allowed_uids

    def handle_error(self, request, client_address) -> None:
        log.exception("connection failed")


def _clear_stale_socket(path: str) -> None:
    if not os.path.exists(path):
        return
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(path)
    except OSError:
        os.unlink(path)
    else:
        raise SystemExit(f"another agentd is already listening on {path}")
    finally:
        probe.close()


def serve(config: Config) -> int:
    procs.harden_daemon()
    os.umask(0o027)
    manager = TurnManager(config)
    server = Server(config, manager)
    done = threading.Event()

    def on_signal(signum, _frame) -> None:
        done.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()
    log.info("agentd listening on %s", config.socket)
    while not done.wait(1.0):
        pass
    log.info("agentd stopping")
    server.shutdown()
    manager.shutdown(timeout=config.kill_grace_seconds + 20.0)
    server.server_close()
    try:
        os.unlink(config.socket)
    except OSError:
        pass
    return 0
