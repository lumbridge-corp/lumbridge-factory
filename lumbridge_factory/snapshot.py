"""The read-only picture of a factory, for `lumbridge-factory status --json` and any screen that shows it
(§14 O7). Ordered by what needs the owner (§11.3). A healthy factory has an empty needs list (`names.NEEDS_KEY`).

`schema` changes only when a field changes meaning; new fields don't bump it.
"""

from __future__ import annotations

from typing import Any

from .core.model import Phase, Ticket
from .effects import Outbox
from .store import Store
from .names import NEEDS_KEY

SCHEMA = 1


def _needs_owner(t: Ticket) -> str | None:
    if t.quarantined:
        return "quarantined"
    if not t.is_open:
        return None
    if t.phase == Phase.WAITING:
        return str(t.waiting_reason or "question")
    if t.phase == Phase.READY:
        return "ready_to_merge"
    return None


def ticket_view(store: Store, t: Ticket) -> dict[str, Any]:
    builder = store.live_builder(t.id)
    active = store.turns(ticket_id=t.id, statuses=("launching", "running"))
    return {
        "id": t.id,
        "repo": t.repo,
        "title": t.title,
        "thread": t.origin_thread,
        "branch": t.branch,
        "status": str(t.status),
        "phase": str(t.phase),
        "end_reason": None if t.end_reason is None else str(t.end_reason),
        "held": t.held,
        "stopped": t.stopped,
        "quarantined": t.quarantined,
        NEEDS_KEY: _needs_owner(t),
        "question": t.question,
        "pr": t.pr if t.pr is not None else t.last_pr,
        "head_sha": t.head_sha,
        "accepted_sha": t.accepted_sha,
        "review_rounds": t.review_rounds,
        "tokens": t.tokens,
        "budget_tokens": t.budget_tokens,
        "wall_seconds": round(t.wall_seconds, 1),
        "budget_wall_seconds": t.budget_wall_seconds,
        "session": None if builder is None else builder.session_id,
        "account": None if builder is None else builder.account,
        "turns_running": [turn["id"] for turn in active],
    }


def _rank(view: dict[str, Any]) -> tuple:
    order = {"quarantined": 0, "question": 1, "trouble": 1, "budget": 1, "rounds": 1, "ready_to_merge": 2}
    if view[NEEDS_KEY]:
        return (0, order.get(view[NEEDS_KEY], 1))
    if view["turns_running"]:
        return (1, 0)
    if view["status"] == "open":
        return (2, 0)
    return (3, 0)


def build(store: Store, generated_at: float) -> dict[str, Any]:
    policy = store.policy()
    views = sorted((ticket_view(store, t) for t in store.tickets()), key=_rank)
    used = store.slots_used()
    return {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "instance": None if policy is None else policy.instance,
        "policy_version": None if policy is None else policy.version,
        "schema_version": store.schema_version,
        "capacity": {
            "slots": None if policy is None else policy.slots,
            "reserved_slots": None if policy is None else policy.reserved_slots,
            "used": used.get("general", 0),
            "used_reserved": used.get("reserved", 0),
        },
        "effects": Outbox(store, lambda: generated_at).counts(),
        NEEDS_KEY: [v["id"] for v in views if v[NEEDS_KEY]],
        "working": [v["id"] for v in views if v["turns_running"]],
        "tickets": views,
        "feed": store.feed(20),
    }
