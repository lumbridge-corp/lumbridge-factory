"""Slots by turn, not by ticket (§7). A pure choice of which waiting turns start now."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .executor import Role

GENERAL = "general"
RESERVED = "reserved"


@dataclass(frozen=True)
class Candidate:
    """An Assignment with something to deliver and no turn running."""

    assignment_id: str
    ticket_id: str
    role: Role
    urgent: bool  # caused by an allowed person's message
    has_session: bool
    position: int  # the oldest waiting input, in arrival order


def priority(c: Candidate) -> tuple:
    """A person's message first, then reviewers so acceptance never starves, then work in
    progress, then new launches. First come, first served within each."""
    if c.urgent:
        rank = 0
    elif c.role == Role.REVIEWER:
        rank = 1
    elif c.has_session:
        rank = 2
    else:
        rank = 3
    return (rank, c.position)


def pick(
    candidates: Sequence[Candidate], used_general: int, used_reserved: int, slots: int, reserved: int
) -> list[tuple[Candidate, str]]:
    free_general = max(0, slots - used_general)
    free_reserved = max(0, reserved - used_reserved)
    chosen: list[tuple[Candidate, str]] = []
    for c in sorted(candidates, key=priority):
        if free_general > 0:
            free_general -= 1
            chosen.append((c, GENERAL))
        elif c.urgent and free_reserved > 0:
            free_reserved -= 1
            chosen.append((c, RESERVED))
    return chosen
