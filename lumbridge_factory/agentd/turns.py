"""Sessions and turns: the part of agentd that starts, watches and stops processes.

The on-disk journal is operational, not domain state (docs/PROTOCOL.md):

    <state>/sessions/<session_id>/  session.json, policy.json, settings.json, role.md
    <state>/turns/<turn_id>/        request.json  written and fsynced before any spawn
                                    events.jsonl, raw.jsonl, stderr.log, inbox.jsonl
                                    result.json   written last; its presence means finished
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Iterator

from lumbridge_factory.core.executor import (
    EventType,
    ExecutorError,
    InboxEntry,
    RunningTurn,
    StartRequest,
    TurnEvent,
    TurnOutcome,
    TurnRequest,
    TurnResult,
    TurnStreamItem,
    entry_from_wire,
    event_from_wire,
    result_from_wire,
    to_wire,
)

from . import childenv, claude, codex, procs, session_files
from .config import Account, Config
from .worktrees import Worktrees, WorktreeError, forget_worktree, pre_trust

_TURN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REQUEST_ID = re.compile(r"^[0-9a-f]{8,64}$")
_SESSION_ID = re.compile(r"^s-[0-9a-f]{20}$")
_MAX_LOG_BYTES = 32 * 1024 * 1024
_STRAGGLER_GRACE = 2.0


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def session_id_for(assignment: str) -> str:
    return "s-" + hashlib.sha256(assignment.encode()).hexdigest()[:20]


class _Inbox:
    """Reads a turn's inbox as the MCP tool appends to it. Tolerates a torn last line."""

    def __init__(self, path: str, secrets: list[str]) -> None:
        self.path = path
        self.secrets = secrets
        self.offset = 0
        self.seq = 0

    def drain(self) -> list[dict]:
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                data = fh.read()
        except FileNotFoundError:
            return []
        end = data.rfind(b"\n")
        if end < 0:
            return []
        self.offset += end + 1
        out = []
        for line in data[: end + 1].splitlines():
            try:
                record = json.loads(childenv.redact(line.decode("utf-8", "replace"), self.secrets))
            except ValueError:
                continue
            if isinstance(record, dict):
                out.append(record)
        return out

    def to_entry(self, record: dict) -> InboxEntry | None:
        """A line that doesn't validate gets no seq. Anything might have written it."""
        try:
            entry = entry_from_wire({**record, "seq": self.seq + 1})
        except (KeyError, ValueError, TypeError):
            return None
        self.seq += 1
        return entry


class Turn:
    def __init__(
        self,
        manager: "TurnManager",
        request: TurnRequest,
        session: dict,
        account: Account,
        turn_dir: str,
    ) -> None:
        self.manager = manager
        self.request = request
        self.session = session
        self.account = account
        self.turn_dir = turn_dir
        self.config = manager.config
        self.cond = threading.Condition()
        self.events: list[dict] = []
        self.result: TurnResult | None = None
        self.stop_reason: TurnOutcome | None = None
        self.entries: list[InboxEntry] = []
        self.secrets: list[str] = []
        self.parser = None
        self.pgid: int | None = None
        self._thread = threading.Thread(target=self._guarded_run, name=f"turn-{request.turn_id}", daemon=True)
        self._seq = 0
        self._events_fh = None
        self._stderr_tail = ""
        self._bytes_logged = 0
        self._wake = threading.Event()

    # --- control -------------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def request_stop(self, reason: TurnOutcome) -> None:
        with self.cond:
            if self.stop_reason is None:
                self.stop_reason = reason
        self._wake.set()

    # --- the stream ----------------------------------------------------------

    def stream(self) -> Iterator[TurnStreamItem]:
        index = 0
        while True:
            with self.cond:
                while index >= len(self.events) and self.result is None:
                    self.cond.wait(1.0)
                batch = self.events[index:]
                index = len(self.events)
                result = self.result
            for item in batch:
                yield event_from_wire(item)
            if result is not None and not batch:
                yield result
                return

    def _emit(self, kind: EventType, data: dict) -> None:
        with self.cond:
            self._seq += 1
            wire = {"turn_id": self.request.turn_id, "seq": self._seq, "type": str(kind), "data": data}
            self.events.append(wire)
            if self._events_fh is not None and (self._bytes_logged < _MAX_LOG_BYTES or kind not in (EventType.TEXT, EventType.TOOL)):
                line = json.dumps(wire, ensure_ascii=False) + "\n"
                self._bytes_logged += len(line)
                self._events_fh.write(line)
                self._events_fh.flush()
            self.cond.notify_all()

    # --- the run -------------------------------------------------------------

    def _guarded_run(self) -> None:
        started = time.monotonic()
        try:
            self._run(started)
        except Exception as exc:  # noqa: BLE001 - a turn must always end in a result
            self._finish(TurnOutcome.FAILED, f"agentd failed to run the turn: {type(exc).__name__}", started)
        finally:
            self.manager._turn_finished(self)

    def _build(self, env: dict[str, str]) -> list[str]:
        session = self.session
        files = session["files"]
        tool = self.config.tools[self.account.tool]
        role = session["role"]
        native = self.request.native_session_id
        if self.account.tool == "claude":
            self.parser = claude.Parser(native_session_id=native)
            return claude.build_argv(
                tool.bin,
                model=self.request.model,
                settings=files["settings"],
                mcp_config=session_files.write_mcp_config(self.turn_dir, role),
                role_file=files["role"],
                native_session_id=native,
            )
        self.parser = codex.Parser(native_session_id=native)
        codex.install_exec_policy(env["CODEX_HOME"])
        return codex.build_argv(
            tool.bin,
            model=self.request.model,
            mcp_argv=session_files.mcp_argv(self.turn_dir, role),
            role_text=Path(files["role"]).read_text(encoding="utf-8"),
            last_message_file=os.path.join(self.turn_dir, "last.txt"),
            git_common_dir=session["common_dir"],
            native_session_id=native,
        )

    def _run(self, started: float) -> None:
        os.makedirs(os.path.join(self.turn_dir, "recall"), exist_ok=True)
        self._events_fh = open(os.path.join(self.turn_dir, "events.jsonl"), "a", encoding="utf-8")
        try:
            env, self.secrets = childenv.turn_env(self.config, self.account, self.request.turn_id)
        except childenv.TokenError as exc:
            self._finish(TurnOutcome.FAILED, str(exc), started)
            return
        argv = self._build(env)
        prompt_path = os.path.join(self.turn_dir, "prompt.txt")
        session_files.write_private(prompt_path, "\n\n".join(i.text for i in self.request.inputs))

        if self._stop_requested():
            self._finish(self.stop_reason, "stopped before the process started", started)
            return
        try:
            with open(prompt_path, "rb") as stdin:
                proc = subprocess.Popen(
                    argv,
                    cwd=self.session["worktree"],
                    env=env,
                    stdin=stdin,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    close_fds=True,
                )
        except OSError as exc:
            self._finish(TurnOutcome.FAILED, f"can't start {os.path.basename(argv[0])}: {exc.strerror}", started)
            return
        env.clear()
        self.pgid = proc.pid

        readers = [
            threading.Thread(target=self._read_stdout, args=(proc.stdout,), daemon=True),
            threading.Thread(target=self._read_stderr, args=(proc.stderr,), daemon=True),
        ]
        for reader in readers:
            reader.start()

        inbox = _Inbox(os.path.join(self.turn_dir, "inbox.jsonl"), self.secrets)
        grace = self.config.kill_grace_seconds
        deadline = started + self.request.limit_seconds
        term_at = kill_at = final_seen = None
        while proc.poll() is None:
            self._drain_inbox(inbox)
            now = time.monotonic()
            if self.stop_reason is None and now >= deadline and not self.parser.done:
                self.request_stop(TurnOutcome.TIMEOUT)
            if self.parser.done and final_seen is None:
                final_seen = now
            # The model is finished, but something it started is keeping the CLI alive.
            lingering = final_seen is not None and now - final_seen >= self.config.post_result_grace_seconds
            if term_at is None and (self.stop_reason is not None or lingering):
                term_at = now
                kill_at = now + grace
                procs.signal_group(proc.pid, 15)
            if kill_at is not None and now >= kill_at:
                procs.signal_group(proc.pid, 9)
            self._wake.wait(0.05)
            self._wake.clear()
        exit_code = proc.returncode

        # Nothing of this turn outlives it, background jobs included.
        procs.sweep(proc.pid, self.request.turn_id, _STRAGGLER_GRACE)
        for reader in readers:
            reader.join(5.0)
        self._drain_inbox(inbox)
        self._close_pipes(proc)

        resumed = bool(self.request.native_session_id)
        outcome, error = self.parser.classify(exit_code, self._stderr_tail[-2000:], resumed)
        if outcome != TurnOutcome.COMPLETED and self.stop_reason is not None:
            outcome = self.stop_reason
            error = {
                TurnOutcome.TIMEOUT: f"the turn passed its limit of {self.request.limit_seconds} s",
                TurnOutcome.STOPPED: "",
                TurnOutcome.INTERRUPTED: "agentd is shutting down",
            }.get(outcome, "")
        self._finish(outcome, childenv.redact(error, self.secrets), started)

    def _stop_requested(self) -> bool:
        with self.cond:
            return self.stop_reason is not None

    @staticmethod
    def _close_pipes(proc: subprocess.Popen) -> None:
        for pipe in (proc.stdout, proc.stderr):
            try:
                pipe.close()
            except OSError:
                pass

    def _read_stdout(self, pipe) -> None:
        raw = open(os.path.join(self.turn_dir, "raw.jsonl"), "a", encoding="utf-8")
        written = 0
        try:
            for bline in pipe:
                line = childenv.redact(bline.decode("utf-8", "replace"), self.secrets)
                if written < _MAX_LOG_BYTES:
                    written += len(line)
                    raw.write(line)
                    raw.flush()
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    for kind, data in self.parser.feed(obj):
                        self._emit(kind, data)
                    if self.parser.done:
                        self._wake.set()
        except ValueError:
            pass  # the pipe closed under us at the end of the turn
        finally:
            raw.close()

    def _read_stderr(self, pipe) -> None:
        log = open(os.path.join(self.turn_dir, "stderr.log"), "a", encoding="utf-8")
        written = 0
        try:
            for bline in pipe:
                line = childenv.redact(bline.decode("utf-8", "replace"), self.secrets)
                self._stderr_tail = (self._stderr_tail + line)[-8192:]
                if written < _MAX_LOG_BYTES:
                    written += len(line)
                    log.write(line)
                    log.flush()
        except ValueError:
            pass
        finally:
            log.close()

    def _drain_inbox(self, inbox: _Inbox) -> None:
        for record in inbox.drain():
            if record.get("kind") == "recall":
                if isinstance(record.get("request_id"), str) and isinstance(record.get("object_id"), str):
                    self._emit(EventType.RECALL_REQUEST, {"request_id": record["request_id"], "object_id": record["object_id"]})
                continue
            entry = inbox.to_entry(record)
            if entry is not None:
                self.entries.append(entry)
                self._emit(EventType.INBOX, json.loads(json.dumps(to_wire(entry))))

    def _finish(self, outcome: TurnOutcome, error: str, started: float) -> None:
        parser = self.parser
        result = TurnResult(
            turn_id=self.request.turn_id,
            session_id=self.request.session_id,
            outcome=outcome,
            native_session_id=parser.native_session_id if parser else self.request.native_session_id,
            tokens=parser.tokens if parser else 0,
            wall_seconds=round(time.monotonic() - started, 3),
            entries=tuple(self.entries),
            error=error[-400:],
        )
        _store_result(self.turn_dir, result)
        with self.cond:
            if self._events_fh is not None:
                self._events_fh.close()
                self._events_fh = None
            self.result = result
            self.cond.notify_all()


def _store_result(turn_dir: str, result: TurnResult) -> None:
    session_files.write_private(os.path.join(turn_dir, "result.json"), json.dumps(to_wire(result), ensure_ascii=False))
    _fsync_dir(turn_dir)


def _load_result(turn_dir: str) -> TurnResult | None:
    try:
        return result_from_wire(json.loads(Path(turn_dir, "result.json").read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None


class TurnManager:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.worktrees = Worktrees(config)
        self.lock = threading.Lock()
        self.live: dict[str, Turn] = {}
        self.sessions_dir = os.path.join(config.state_dir, "sessions")
        self.turns_dir = os.path.join(config.state_dir, "turns")
        for path in (config.state_dir, self.sessions_dir, self.turns_dir):
            os.makedirs(path, mode=0o700, exist_ok=True)
        os.makedirs(os.path.join(config.agent_home, "tmp"), exist_ok=True)
        self.tool_problems = self._check_tools()
        self._recover()

    def _check_tools(self) -> dict[str, str]:
        problems = {}
        # Claude runs its hooks through this wrapper. A hook that can't start exits 127,
        # which Claude Code treats as "carry on": the policy would silently fail open.
        for path in (childenv.SCRUB_SHELL, session_files.HOOK_PATH, session_files.MCP_PATH):
            if not os.access(path, os.R_OK) or (path == childenv.SCRUB_SHELL and not os.access(path, os.X_OK)):
                problems["claude"] = f"{path.name} is missing or not executable; refusing to run without the policy"
        for name, tool in self.config.tools.items():
            if not tool.enabled or not tool.expected_version or name in problems:
                continue
            try:
                out = subprocess.run(
                    [tool.bin, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=childenv.base_env(self.config),
                    stdin=subprocess.DEVNULL,
                ).stdout
            except (OSError, subprocess.SubprocessError) as exc:
                problems[name] = f"{tool.bin} can't run: {type(exc).__name__}"
                continue
            if tool.expected_version not in out:
                problems[name] = f"{tool.bin} is not the pinned version {tool.expected_version}"
        return problems

    def _recover(self) -> None:
        """A journal entry without a result is a turn the last agentd lost: it's interrupted."""
        for turn_id in sorted(os.listdir(self.turns_dir)):
            turn_dir = os.path.join(self.turns_dir, turn_id)
            if os.path.exists(os.path.join(turn_dir, "result.json")):
                continue
            procs.kill_marked(turn_id)
            result = self._interrupted_result(turn_id, turn_dir, "agentd restarted during this turn")
            if result is not None:
                _store_result(turn_dir, result)
            else:
                shutil.rmtree(turn_dir, ignore_errors=True)

    def _interrupted_result(self, turn_id: str, turn_dir: str, why: str) -> TurnResult | None:
        try:
            request = json.loads(Path(turn_dir, "request.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None  # died before the journal write finished, so nothing was spawned
        native = request.get("native_session_id")
        tokens = 0
        try:
            with open(os.path.join(turn_dir, "events.jsonl"), encoding="utf-8") as fh:
                for line in fh:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("type") == str(EventType.STARTED):
                        native = event.get("data", {}).get("native_session_id") or native
                    elif event.get("type") == str(EventType.USAGE):
                        tokens = int(event.get("data", {}).get("tokens") or 0)
        except OSError:
            pass
        inbox = _Inbox(os.path.join(turn_dir, "inbox.jsonl"), [])
        entries = []
        for record in inbox.drain():
            if record.get("kind") != "recall":
                entry = inbox.to_entry(record)
                if entry is not None:
                    entries.append(entry)
        return TurnResult(
            turn_id=turn_id,
            session_id=request.get("session_id", ""),
            outcome=TurnOutcome.INTERRUPTED,
            native_session_id=native,
            tokens=tokens,
            entries=tuple(entries),
            error=why,
        )

    # --- sessions ------------------------------------------------------------

    def _session_dir(self, session_id: str) -> str:
        if not _SESSION_ID.match(session_id):
            raise ExecutorError("unknown_session", f"bad session id {session_id!r}", retryable=False)
        return os.path.join(self.sessions_dir, session_id)

    def _load_session(self, session_id: str) -> dict:
        try:
            return json.loads(Path(self._session_dir(session_id), "session.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ExecutorError("unknown_session", session_id, retryable=False) from None

    def _usable_account(self, name: str) -> Account:
        account = self.config.accounts.get(name)
        if account is None:
            raise ExecutorError("bad_request", f"unknown account {name!r}", retryable=False)
        tool = self.config.tools[account.tool]
        if not tool.enabled:
            raise ExecutorError("unavailable", f"{account.tool} is disabled on this agentd", retryable=False)
        problem = self.tool_problems.get(account.tool)
        if problem:
            raise ExecutorError("unavailable", problem, retryable=False)
        return account

    def start_session(self, request: StartRequest) -> str:
        self._usable_account(request.account)
        session_id = session_id_for(request.assignment)
        session_dir = self._session_dir(session_id)
        marker = os.path.join(session_dir, "session.json")
        if os.path.exists(marker):
            return session_id  # idempotent, and needs no network on a re-drive
        os.makedirs(session_dir, mode=0o700, exist_ok=True)
        try:
            prepared = self.worktrees.prepare(request.worktree)
        except WorktreeError as exc:
            raise ExecutorError(exc.code, exc.message, retryable=exc.retryable) from exc
        pre_trust(self.config.agent_home, prepared.path)
        files = session_files.write_session_files(self.config, request, prepared, session_dir)
        session = {
            "session_id": session_id,
            "ticket": request.ticket,
            "assignment": request.assignment,
            "role": str(request.role),
            "worktree": prepared.path,
            "common_dir": prepared.common_dir,
            "branch": prepared.branch,
            "files": files,
        }
        session_files.write_private(marker, json.dumps(session, indent=2))
        _fsync_dir(session_dir)
        return session_id

    # --- turns ---------------------------------------------------------------

    def turn(self, request: TurnRequest) -> Iterator[TurnStreamItem]:
        if not _TURN_ID.match(request.turn_id):
            raise ExecutorError("bad_request", "bad turn_id", retryable=False)
        turn_dir = os.path.join(self.turns_dir, request.turn_id)
        with self.lock:
            live = self.live.get(request.turn_id)
            if live is not None:
                return live.stream()
            finished = self._finished_or_lost(request.turn_id, turn_dir)
            if finished is not None:
                return self._replay(turn_dir, finished)
            session = self._load_session(request.session_id)
            account = self._usable_account(request.account)
            if not request.inputs:
                raise ExecutorError("bad_request", "a turn needs at least one input", retryable=False)
            if request.limit_seconds <= 0:
                raise ExecutorError("bad_request", "limit_seconds must be positive", retryable=False)
            for other in self.live.values():
                if other.request.session_id == request.session_id or other.session["worktree"] == session["worktree"]:
                    raise ExecutorError("unavailable", "this session or worktree has a running turn", retryable=True)
            if len(self.live) >= self.config.max_turns:
                raise ExecutorError("unavailable", "agentd is at max_turns", retryable=True)
            # Durable before anything can spawn: a crash from here on replays as interrupted.
            os.makedirs(turn_dir, mode=0o700)
            session_files.write_private(os.path.join(turn_dir, "request.json"), json.dumps(to_wire(request), ensure_ascii=False))
            _fsync_dir(turn_dir)
            _fsync_dir(self.turns_dir)
            turn = Turn(self, request, session, account, turn_dir)
            self.live[request.turn_id] = turn
            turn.start()
            return turn.stream()

    def _finished_or_lost(self, turn_id: str, turn_dir: str) -> TurnResult | None:
        result = _load_result(turn_dir)
        if result is not None or not os.path.isdir(turn_dir):
            return result
        result = self._interrupted_result(turn_id, turn_dir, "this turn has no live process")
        if result is None:
            shutil.rmtree(turn_dir, ignore_errors=True)
            return None
        _store_result(turn_dir, result)
        return result

    def _replay(self, turn_dir: str, result: TurnResult) -> Iterator[TurnStreamItem]:
        try:
            with open(os.path.join(turn_dir, "events.jsonl"), encoding="utf-8") as fh:
                for line in fh:
                    try:
                        yield event_from_wire(json.loads(line))
                    except (ValueError, KeyError):
                        continue
        except OSError:
            pass
        yield result

    def _turn_finished(self, turn: Turn) -> None:
        with self.lock:
            if self.live.get(turn.request.turn_id) is turn:
                del self.live[turn.request.turn_id]

    def stop(self, session_id: str) -> None:
        with self.lock:
            turns = [t for t in self.live.values() if t.request.session_id == session_id]
        for turn in turns:
            turn.request_stop(TurnOutcome.STOPPED)

    def running(self) -> list[RunningTurn]:
        with self.lock:
            return [RunningTurn(t.request.turn_id, t.request.session_id) for t in self.live.values()]

    def collect(self, turn_id: str) -> TurnResult | None:
        if not _TURN_ID.match(turn_id):
            raise ExecutorError("bad_request", "bad turn_id", retryable=False)
        return _load_result(os.path.join(self.turns_dir, turn_id))

    def forget(self, turn_id: str) -> None:
        if not _TURN_ID.match(turn_id):
            raise ExecutorError("bad_request", "bad turn_id", retryable=False)
        with self.lock:
            if turn_id in self.live:
                return
            shutil.rmtree(os.path.join(self.turns_dir, turn_id), ignore_errors=True)

    def answer_recall(self, session_id: str, request_id: str, text: str) -> None:
        if not _REQUEST_ID.match(request_id):
            raise ExecutorError("bad_request", "bad request_id", retryable=False)
        with self.lock:
            turns = [t for t in self.live.values() if t.request.session_id == session_id]
        for turn in turns:  # none if the turn ended; the tool has already gone
            session_files.write_private(os.path.join(turn.turn_dir, "recall", request_id), text)

    def remove_session(self, session_id: str, worktree: bool) -> None:
        """Disk doesn't grow forever (PROTOCOL.md). The marker goes last, so a crash part way
        through repeats the rest. Local work that wasn't pushed goes with the worktree."""
        session_dir = self._session_dir(session_id)
        try:
            session = self._load_session(session_id)
        except ExecutorError:
            shutil.rmtree(session_dir, ignore_errors=True)
            return  # never existed, or already gone
        path = session["worktree"]
        with self.lock:
            for turn in self.live.values():
                if turn.request.session_id == session_id or (worktree and turn.session["worktree"] == path):
                    raise ExecutorError("unavailable", "this session or worktree has a running turn", retryable=True)
        if worktree:
            try:
                self.worktrees.remove(path, session["common_dir"], session.get("branch") or "")
            except WorktreeError as exc:
                raise ExecutorError(exc.code, exc.message, retryable=exc.retryable) from exc
            forget_worktree(self.config.agent_home, path)
        shutil.rmtree(session_dir, ignore_errors=True)

    def shutdown(self, timeout: float = 30.0) -> None:
        """SIGTERM: every running turn ends `interrupted`, with its result written."""
        with self.lock:
            turns = list(self.live.values())
        for turn in turns:
            turn.request_stop(TurnOutcome.INTERRUPTED)
        deadline = time.monotonic() + timeout
        for turn in turns:
            turn.join(max(0.0, deadline - time.monotonic()))
