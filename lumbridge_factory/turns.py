"""Carry turn streams from an Executor to the engine's single writer.

A turn lasts up to 45 minutes and the engine must not wait for it, so a runner consumes the
stream and the engine drains what has arrived on each tick. A runner holds no state worth
keeping: if it dies, recovery asks the executor.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Iterator, Protocol

from .core.executor import Executor, ExecutorError, TurnRequest, TurnResult, TurnStreamItem


class _Hold:
    """Only from a fake: "no more yet". A real stream just blocks."""


HOLD = _Hold()


@dataclass(frozen=True)
class StreamBroken:
    """The stream ended without a TurnResult. The turn may still be running."""

    turn_id: str
    session_id: str
    error: str
    retryable: bool = True


class Runner(Protocol):
    def launch(self, executor: Executor, request: TurnRequest) -> None:
        """Start consuming this turn. Safe to repeat: turn() attaches by turn_id."""

    def drain(self) -> list[TurnStreamItem | StreamBroken]: ...

    def is_live(self, turn_id: str) -> bool: ...


class InlineRunner:
    """Single-threaded and deterministic: drain() advances each stream."""

    def __init__(self) -> None:
        self._live: dict[str, tuple[Iterator, TurnRequest]] = {}

    def launch(self, executor: Executor, request: TurnRequest) -> None:
        if request.turn_id not in self._live:
            self._live[request.turn_id] = (iter(executor.turn(request)), request)

    def is_live(self, turn_id: str) -> bool:
        return turn_id in self._live

    def drain(self) -> list[TurnStreamItem | StreamBroken]:
        out: list[TurnStreamItem | StreamBroken] = []
        for turn_id, (stream, request) in list(self._live.items()):
            try:
                while True:
                    item = next(stream)
                    if item is HOLD:
                        break
                    out.append(item)
                    if isinstance(item, TurnResult):
                        del self._live[turn_id]
                        break
            except StopIteration:
                del self._live[turn_id]
                out.append(StreamBroken(turn_id, request.session_id, "stream ended without a result"))
            except ExecutorError as exc:
                del self._live[turn_id]
                out.append(StreamBroken(turn_id, request.session_id, str(exc), exc.retryable))
        return out


class ThreadRunner:
    """The real one: a daemon thread per live turn, all feeding one queue."""

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._live: set[str] = set()
        self._lock = threading.Lock()

    def launch(self, executor: Executor, request: TurnRequest) -> None:
        with self._lock:
            if request.turn_id in self._live:
                return
            self._live.add(request.turn_id)
        threading.Thread(target=self._consume, args=(executor, request), daemon=True, name=f"turn-{request.turn_id}").start()

    def is_live(self, turn_id: str) -> bool:
        with self._lock:
            return turn_id in self._live

    def _consume(self, executor: Executor, request: TurnRequest) -> None:
        last: TurnStreamItem | StreamBroken
        last = StreamBroken(request.turn_id, request.session_id, "stream ended without a result")
        try:
            for item in executor.turn(request):
                if item is HOLD:
                    time.sleep(0.01)
                elif isinstance(item, TurnResult):
                    last = item
                    break
                else:
                    self._queue.put(item)
        except ExecutorError as exc:
            last = StreamBroken(request.turn_id, request.session_id, str(exc), exc.retryable)
        except Exception as exc:  # a dead socket, bad JSON: ask the executor what became of it
            last = StreamBroken(request.turn_id, request.session_id, f"{type(exc).__name__}: {exc}")
        with self._lock:
            self._live.discard(request.turn_id)
        self._queue.put(last)

    def drain(self) -> list[TurnStreamItem | StreamBroken]:
        out = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                return out
