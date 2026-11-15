"""The domain nouns of ARCHITECTURE §4. Plain frozen values; no I/O."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .executor import Role, WorktreeSpec


class TriggerKind(StrEnum):
    # Written by a person (or a bot acting like one): subject to the allowed list.
    ISSUE_OPENED = "issue_opened"
    ISSUE_LABELED = "issue_labeled"
    COMMENT = "comment"
    REVIEW = "review"
    COMMAND = "command"  # a signed office button, or the local CLI
    # Facts reported by a Source about the world. They wake the gates; they grant nothing.
    CI_STATUS = "ci_status"
    PR_STATE = "pr_state"
    MAIN_MOVED = "main_moved"
    TIMER = "timer"
    # Written by the engine itself: a failed check, a posted review.
    INTERNAL = "internal"


PERSON_KINDS = frozenset(
    {TriggerKind.ISSUE_OPENED, TriggerKind.ISSUE_LABELED, TriggerKind.COMMENT, TriggerKind.REVIEW, TriggerKind.COMMAND}
)


class ActorKind(StrEnum):
    PERSON = "person"
    BOT = "bot"
    SYSTEM = "system"


class Decision(StrEnum):
    ACCEPT = "accept"
    ATTACH = "attach"
    STOP = "stop"
    IGNORE = "ignore"
    UNAVAILABLE = "unavailable"


class Strength(StrEnum):
    EXPLICIT = "explicit"
    INFERRED = "inferred"


class RuleMode(StrEnum):
    OFF = "off"
    OBSERVE = "observe"
    AUTO = "auto"


class TicketStatus(StrEnum):
    OPEN = "open"
    ENDED = "ended"


class Phase(StrEnum):
    BUILDING = "building"  # the builder has work, running or about to
    GATING = "gating"  # waiting on CI, the reviewer or main; holds no slot
    READY = "ready"  # all three gates passed on one SHA: ready for the owner
    WAITING = "waiting"  # one question is open with the owner; holds no slot


class EndReason(StrEnum):
    MERGED = "merged"
    CLOSED = "closed"
    DROPPED = "dropped"
    STOPPED = "stopped"


class WaitReason(StrEnum):
    QUESTION = "question"  # the worker asked
    BUDGET = "budget"  # §7: reaching a budget asks instead of killing
    ROUNDS = "rounds"  # §5.5: a 4th review round
    TROUBLE = "trouble"  # a turn failed, or repeated recoveries, or no report


class SubMode(StrEnum):
    OWNER = "owner"  # The owner's words here wake the builder
    CONTEXT = "context"  # delivered with the next turn; wakes nothing


@dataclass(frozen=True)
class Trigger:
    """One immutable event. Identity is (source, external_id)."""

    source: str
    external_id: str
    thread: str
    kind: TriggerKind
    actor: str
    actor_kind: ActorKind = ActorKind.PERSON
    body: str = ""
    repo: str = ""
    at: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return trigger_id(self.source, self.external_id)


def trigger_id(source: str, external_id: str) -> str:
    return "tr-" + hashlib.sha256(f"{source}\0{external_id}".encode()).hexdigest()[:12]


def ticket_id(trigger: Trigger) -> str:
    return "t-" + hashlib.sha256(f"{trigger.source}\0{trigger.external_id}".encode()).hexdigest()[:8]


# Thread keys. A Thread is one object: an issue, a PR, a PR's CI, main's CI, or an office command.
def issue_thread(repo: str, number: int) -> str:
    return f"{repo}#issue/{number}"


def pr_thread(repo: str, number: int) -> str:
    return f"{repo}#pr/{number}"


def ci_thread(repo: str, number: int) -> str:
    return f"{repo}#pr/{number}/ci"


def main_ci_thread(repo: str) -> str:
    return f"{repo}#main/ci"


def is_chat_thread(thread: str) -> bool:
    """Chat channels carry the 100-word reply limit (§6). Issue and PR comments don't."""
    return thread.startswith(("office:", "chat:"))


@dataclass(frozen=True)
class Mandate:
    """What an accept grants. Immutable: wider scope means a new Ticket."""

    id: str
    ticket_id: str
    policy_version: int
    repo: str
    base: str
    origin_thread: str
    strength: Strength
    rules: tuple[str, ...]
    requested_by: str
    trigger_id: str
    budget_tokens: int
    budget_wall_seconds: int
    created_at: float


@dataclass(frozen=True)
class Ticket:
    id: str
    repo: str
    base: str
    branch: str
    title: str
    origin_thread: str
    mandate_id: str
    requested_by: str
    created_at: float = 0.0
    status: TicketStatus = TicketStatus.OPEN
    phase: Phase = Phase.BUILDING
    end_reason: EndReason | None = None
    held: bool = False
    stopped: bool = False
    quarantined: bool = False
    waiting_reason: WaitReason | None = None
    question: str | None = None
    pr: int | None = None
    pr_claim: int | None = None  # a builder's word, not yet checked against the forge
    pr_opens: int = 0  # how many times the factory was asked to open a PR for this Ticket
    worktree_gone: bool = False  # cleaned up after it ended; a resume starts a new session
    last_pr: int | None = None
    head_sha: str | None = None
    accepted_sha: str | None = None
    ci_red_sha: str | None = None  # the SHA the builder was last woken about
    review_requested_sha: str | None = None
    changes_sha: str | None = None
    rereview: bool = False  # The owner spoke after a rejection of the head: it gets a fresh review
    review_floor: int = -1  # verdicts at or below this seq were overruled
    passed_change: str | None = None  # "<sha> <change>": the head a review passed on, and its diff's fingerprint then
    update_requested: str | None = None  # "<head>:<main>" the last branch update was for
    review_rounds: int = 0
    strikes: int = 0  # turns ended without a useful report
    recoveries: int = 0  # cut-off turns in a row
    transient: int = 0  # transient failures in a row
    next_turn_at: float = 0.0
    tokens: int = 0
    wall_seconds: float = 0.0
    budget_tokens: int = 0
    budget_wall_seconds: int = 0
    base_budget_tokens: int = 0
    base_budget_wall_seconds: int = 0

    @property
    def is_open(self) -> bool:
        return self.status == TicketStatus.OPEN

    @property
    def over_budget(self) -> bool:
        return self.tokens >= self.budget_tokens or self.wall_seconds >= self.budget_wall_seconds


@dataclass(frozen=True)
class Assignment:
    id: str
    ticket_id: str
    role: Role
    worktree: WorktreeSpec
    model: str
    account: str = ""
    session_id: str | None = None
    native_session_id: str | None = None
    live: bool = True
    created_at: float = 0.0


@dataclass(frozen=True)
class RouterDecision:
    """The Router's answer (§4.2). `note` is at most 30 words; it never restates anything."""

    decision: Decision
    would_accept: bool = False
    ticket_id: str | None = None  # for attach, stop
    note: str = ""
    title: str = ""


def clamp_words(text: str, limit: int) -> str:
    words = text.split()
    return text if len(words) <= limit else " ".join(words[:limit])
