"""The contract between factoryd and an executor (agentd, or a fake).

ARCHITECTURE §6. This module is shared: factoryd imports it, agentd imports it, and
docs/PROTOCOL.md specifies the same shapes on the wire. It does no I/O.

Vocabulary:
  session_id         the executor's handle for one Assignment, returned by start().
  native_session_id  what the agent CLI printed (claude's or codex's own id). factoryd
                     stores it and passes it back on every turn, so agentd keeps no
                     domain state.
  turn_id            chosen by factoryd. turn() is idempotent on it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Iterator, Protocol, Union, runtime_checkable

PROTOCOL_VERSION = 1


class Role(StrEnum):
    BUILDER = "builder"
    REVIEWER = "reviewer"


class InputKind(StrEnum):
    ASSIGNMENT = "assignment"  # the first message of a session
    TRIGGER = "trigger"  # an outside event, already fenced as data
    RECOVERY = "recovery"  # the recovery note after a cut-off turn (§8)
    NOTE = "note"  # a message from factoryd itself


class TurnOutcome(StrEnum):
    COMPLETED = "completed"  # the model ended its turn
    INTERRUPTED = "interrupted"  # the process died: host or agentd restart, kill
    TRANSIENT = "transient"  # HTTP 408/429/5xx, connection reset, provider overload
    ACCOUNT_LIMIT = "account_limit"  # this account hit its limit; another may work
    RESUME_FAILED = "resume_failed"  # the native session can't be resumed
    TIMEOUT = "timeout"  # the wall-clock limit passed and agentd killed the turn
    FAILED = "failed"  # anything else that is the agent's or environment's fault
    STOPPED = "stopped"  # stop() was called during this turn


class ReportStatus(StrEnum):
    PROGRESS = "progress"
    WAITING = "waiting"
    READY = "ready"
    BLOCKED = "blocked"
    FAILED = "failed"
    DROPPED = "dropped"


class Verdict(StrEnum):
    PASS = "pass"
    CHANGES_REQUESTED = "changes_requested"


class EntryKind(StrEnum):
    """What the worker's tools (§6) can append to an Assignment's inbox."""

    REPORT = "report"
    REPLY = "reply"
    FOLLOW = "follow"
    UNRELATED = "unrelated"
    HANDBACK = "handback"


class EventType(StrEnum):
    STARTED = "started"  # data: {native_session_id?}
    TEXT = "text"  # data: {text}; opaque, never parsed for state
    TOOL = "tool"  # data: {name}; opaque, for the watchdog
    USAGE = "usage"  # data: {tokens}; running total for this turn
    INBOX = "inbox"  # data: an InboxEntry as a dict
    RECALL_REQUEST = "recall_request"  # data: {request_id, object_id}


class ExecutorError(Exception):
    """A protocol-level failure. `retryable` means: try the same call again later."""

    def __init__(self, code: str, message: str = "", retryable: bool = True):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class WorktreeSpec:
    repo: str  # "acme/widgets"
    name: str  # directory name under the factory's worktrees dir
    branch: str  # "factory/<ticket-short>"; empty for a reviewer
    base: str = "main"
    pinned_sha: str | None = None  # reviewer: check out exactly this commit
    read_only: bool = False  # reviewer: true


@dataclass(frozen=True)
class PromptParts:
    """Trusted text only: built-in prompts and a matched rule's instructions (§4.3).

    agentd puts `system` in the system prompt. Nothing from outside ever goes here;
    outside text arrives through TurnInput, fenced.
    """

    system: str
    rule_instructions: str = ""


@dataclass(frozen=True)
class StartRequest:
    ticket: str
    assignment: str
    role: Role
    worktree: WorktreeSpec
    prompt_parts: PromptParts
    account: str
    model: str


@dataclass(frozen=True)
class TurnInput:
    kind: InputKind
    text: str  # rendered by factoryd; deliver verbatim, in order, as the user message
    ref: str = ""  # the Trigger id it came from, when any


@dataclass(frozen=True)
class TurnRequest:
    turn_id: str
    session_id: str
    native_session_id: str | None  # None on the session's first turn
    inputs: tuple[TurnInput, ...]
    account: str
    model: str
    limit_seconds: int = 45 * 60


@dataclass(frozen=True)
class Report:
    """A structured status (§4). Free text is never parsed for state.

    A reviewer finishes with status=ready plus a verdict and findings. factoryd treats
    all of this as a claim: it re-checks everything before using it (§6).
    """

    status: ReportStatus
    summary: str = ""
    pr: int | None = None
    question: str | None = None
    verdict: Verdict | None = None
    findings: str = ""


@dataclass(frozen=True)
class InboxEntry:
    """One tool call by the worker. `seq` counts from 1 within a turn."""

    seq: int
    kind: EntryKind
    report: Report | None = None  # kind=report
    purpose: str = ""  # kind=reply
    text: str = ""  # kind=reply; kind=handback: the reason
    thread: str = ""  # kind=follow, unrelated
    trigger: str = ""  # kind=handback


@dataclass(frozen=True)
class TurnEvent:
    turn_id: str
    seq: int
    type: EventType
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TurnResult:
    """Always the last item of a turn's stream."""

    turn_id: str
    session_id: str
    outcome: TurnOutcome
    native_session_id: str | None = None
    tokens: int = 0
    wall_seconds: float = 0.0
    entries: tuple[InboxEntry, ...] = ()  # the turn's complete inbox, in seq order
    error: str = ""  # a short tail for a person to read; never parsed for state


@dataclass(frozen=True)
class RunningTurn:
    turn_id: str
    session_id: str


TurnStreamItem = Union[TurnEvent, TurnResult]


@runtime_checkable
class Executor(Protocol):
    def start(self, request: StartRequest) -> str:
        """Prepare the worktree and config; return the session_id. No model runs.

        Idempotent on request.assignment: the same assignment returns the same id and
        keeps the existing worktree.
        """

    def turn(self, request: TurnRequest) -> Iterator[TurnStreamItem]:
        """Run one turn, yielding events and then one TurnResult.

        Idempotent on request.turn_id: if that turn is running, attach to it; if it has
        finished, replay its result; if it was never seen, start it. A turn cut off by an
        agentd restart is never restarted: it replays as INTERRUPTED. Before spawning,
        agentd must durably record the turn_id.
        """

    def stop(self, session_id: str) -> None:
        """Kill the session's running turn, if any. Idempotent; not an error if idle."""

    def running(self) -> list[RunningTurn]:
        """Every turn alive right now. factoryd calls this after any restart."""

    def collect(self, turn_id: str) -> TurnResult | None:
        """The stored result of a finished turn, or None if unfinished or never seen."""

    def forget(self, turn_id: str) -> None:
        """factoryd has durably stored this result; agentd may delete it."""

    def answer_recall(self, session_id: str, request_id: str, text: str) -> None:
        """Deliver the answer to a RECALL_REQUEST to the waiting MCP tool."""

    def remove_session(self, session_id: str, worktree: bool) -> None:
        """Delete a session's files and, if `worktree`, its worktree and local branch. Idempotent.
        Raises a retryable `unavailable` while a turn is running there."""


# --- wire codec -----------------------------------------------------------------
# Plain dicts, the same on the socket and in the store. Unknown keys are ignored so
# either side can add an optional field without a protocol bump.


def to_wire(value: Any) -> dict[str, Any]:
    return asdict(value)


def _opt(enum_type, value):
    return None if value is None else enum_type(value)


def report_from_wire(d: dict[str, Any]) -> Report:
    pr = d.get("pr")
    return Report(
        status=ReportStatus(d["status"]),
        summary=str(d.get("summary") or ""),
        pr=None if pr is None else int(pr),
        question=None if d.get("question") is None else str(d["question"]),
        verdict=_opt(Verdict, d.get("verdict")),
        findings=str(d.get("findings") or ""),
    )


def entry_from_wire(d: dict[str, Any]) -> InboxEntry:
    return InboxEntry(
        seq=int(d["seq"]),
        kind=EntryKind(d["kind"]),
        report=None if d.get("report") is None else report_from_wire(d["report"]),
        purpose=str(d.get("purpose") or ""),
        text=str(d.get("text") or ""),
        thread=str(d.get("thread") or ""),
        trigger=str(d.get("trigger") or ""),
    )


def event_from_wire(d: dict[str, Any]) -> TurnEvent:
    return TurnEvent(turn_id=d["turn_id"], seq=int(d["seq"]), type=EventType(d["type"]), data=dict(d.get("data") or {}))


def result_from_wire(d: dict[str, Any]) -> TurnResult:
    return TurnResult(
        turn_id=d["turn_id"],
        session_id=d["session_id"],
        outcome=TurnOutcome(d["outcome"]),
        native_session_id=d.get("native_session_id"),
        tokens=int(d.get("tokens") or 0),
        wall_seconds=float(d.get("wall_seconds") or 0.0),
        entries=tuple(entry_from_wire(e) for e in d.get("entries") or ()),
        error=str(d.get("error") or ""),
    )


def start_request_from_wire(d: dict[str, Any]) -> StartRequest:
    w, p = d["worktree"], d["prompt_parts"]
    return StartRequest(
        ticket=d["ticket"],
        assignment=d["assignment"],
        role=Role(d["role"]),
        worktree=WorktreeSpec(
            repo=w["repo"],
            name=w["name"],
            branch=w.get("branch") or "",
            base=w.get("base") or "main",
            pinned_sha=w.get("pinned_sha"),
            read_only=bool(w.get("read_only")),
        ),
        prompt_parts=PromptParts(system=p["system"], rule_instructions=p.get("rule_instructions") or ""),
        account=d["account"],
        model=d["model"],
    )


def turn_request_from_wire(d: dict[str, Any]) -> TurnRequest:
    return TurnRequest(
        turn_id=d["turn_id"],
        session_id=d["session_id"],
        native_session_id=d.get("native_session_id"),
        inputs=tuple(
            TurnInput(kind=InputKind(i["kind"]), text=i["text"], ref=i.get("ref") or "") for i in d.get("inputs") or ()
        ),
        account=d["account"],
        model=d["model"],
        limit_seconds=int(d.get("limit_seconds") or 45 * 60),
    )
