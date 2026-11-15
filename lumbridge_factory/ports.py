"""The engine's other outside interfaces. Fakes in fakes.py; the real ones live in their own modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from .core.executor import Role
from .core.model import RouterDecision, Trigger


class CannotCheck(Exception):
    """The channel can't say whether a write already happened."""


class Source(Protocol):
    """A stream of events with its own cursor (§4). Drops the bot's own events itself."""

    name: str

    def poll(self, cursor: str | None) -> tuple[Sequence[Trigger], str]: ...


class Channel(Protocol):
    """The write side of a Thread (§4). `find_*` lets a retry check before writing (§8): a real
    channel stores the key in a hidden footer. Raise CannotCheck if it can't look."""

    def find_comment(self, thread: str, key: str) -> bool: ...

    def post_comment(self, thread: str, text: str, key: str) -> None: ...

    def find_review(self, repo: str, pr: int, key: str) -> bool: ...

    def post_review(self, repo: str, pr: int, sha: str, verdict: str, findings: str, key: str) -> None: ...

    def update_branch(self, repo: str, pr: int) -> str:
        """Ask the forge to merge base into the PR's branch. Returns "updated" or "conflict".
        Must be safe to repeat."""

    def ensure_pr(self, repo: str, branch: str, base: str, title: str, body: str, key: str) -> dict:
        """The open PR from branch into base, opening one if there is none. Returns {"pr": number},
        or {"refused": why} when the forge says there is nothing to open. Safe to repeat."""


class AccountRouter(Protocol):
    """§7: which account a turn runs on. accounts.StaticAccounts is the built-in one; an add-on can supply another
    (accounts.load_provider). It may also offer `used_pct(account) -> float | None`, which `tight_account` reads."""

    def pick(self, role: Role, avoid: str = "") -> str | None:
        """A usable account, preferring not `avoid`; None if all are at their limit."""

    def mark_limited(self, account: str) -> None: ...


@dataclass(frozen=True)
class Assessment:
    eval_id: str
    rule: str
    thread: str
    cards: tuple[str, ...]  # the Triggers, verbatim and fenced


class Assessor(Protocol):
    """The Router (§4.2). There is no model-backed one yet. Raising, or returning UNAVAILABLE, retries later."""

    def assess(self, request: Assessment) -> RouterDecision: ...
