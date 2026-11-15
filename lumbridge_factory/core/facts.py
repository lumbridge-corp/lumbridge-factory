"""What the forge says, read fresh by the host (§1.3). Workers never supply these."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CiState(StrEnum):
    PENDING = "pending"
    GREEN = "green"
    RED = "red"


class ReviewState(StrEnum):
    NONE = "none"
    PASS = "pass"
    CHANGES_REQUESTED = "changes_requested"


class PrState(StrEnum):
    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"


@dataclass(frozen=True)
class PrFacts:
    number: int
    state: PrState
    head_sha: str
    head_branch: str
    base: str
    # A fingerprint of the PR's diff against its base at head_sha (Gate 2's carried review); None when not read or unsure.
    change: str | None = None


@dataclass(frozen=True)
class CiFailure:
    context: str
    log_tail: str = ""


@dataclass(frozen=True)
class CiFacts:
    """Every required context on exactly this SHA. Green means all, from the right runner."""

    sha: str
    state: CiState
    failures: tuple[CiFailure, ...] = ()


@dataclass(frozen=True)
class ReviewFacts:
    """The newest review on exactly this SHA."""

    sha: str
    state: ReviewState
    reviewer: str = ""
    findings: str = ""
    seq: int = 0  # grows with each verdict on the forge, so "newer than" can be asked


@dataclass(frozen=True)
class MainFacts:
    main_sha: str
    contains_main: bool


@dataclass(frozen=True)
class GateFacts:
    """One read of all three gates. `pr` is None when the forge has no such PR."""

    number: int
    pr: PrFacts | None = None
    ci: CiFacts | None = None
    review: ReviewFacts | None = None
    main: MainFacts | None = None
