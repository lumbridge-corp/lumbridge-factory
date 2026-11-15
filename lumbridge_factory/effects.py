"""Stored-then-done effects (§8): the Outbox.

Every outside effect is a row written in the same transaction as the decision that needs it,
and is performed after commit. Keys come from the effect's inputs, so enqueueing the same thing
twice is one row. A crash between commit and effect leaves the row pending. A crash between
effect and bookkeeping re-runs the handler, so handlers check before they write.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .store import Store

BACKOFF_START = 2.0
BACKOFF_MAX = 300.0


class UnknownOutcome(Exception):
    """The write may or may not have happened, and it can't be checked. The only way to
    quarantine (§8)."""


@dataclass(frozen=True)
class EffectRow:
    id: int
    key: str
    kind: str
    ticket_id: str | None
    payload: dict[str, Any]
    attempts: int


Handler = Callable[[EffectRow], Any]
# Called inside the transaction that marks the row done, so a result is never half applied.
Completion = Callable[[EffectRow, Any], None]
Uncertain = Callable[[EffectRow, str], None]


def backoff(attempts: int) -> float:
    # The exponent is capped: an effect that fails for days (seen in production: update_branch on a merged PR, 422 every
    # 5 minutes) passes 1024 attempts, and 2.0 * 2 ** 1024 is an OverflowError that failed every tick from then on.
    return min(BACKOFF_MAX, BACKOFF_START * (2 ** min(32, max(0, attempts - 1))))


class Outbox:
    def __init__(self, store: Store, clock: Callable[[], float] = time.time):
        self.store = store
        self.clock = clock

    def enqueue(self, kind: str, key: str, payload: dict[str, Any], ticket_id: str | None = None) -> bool:
        """Call inside a transaction. False if this key already exists."""
        cur = self.store.conn.execute(
            "INSERT OR IGNORE INTO outbox (key, kind, ticket_id, payload, status, next_at, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (key, kind, ticket_id, json.dumps(payload, sort_keys=True), self.clock(), self.clock()),
        )
        return cur.rowcount == 1

    def cancel(self, key: str) -> None:
        self.store.conn.execute("UPDATE outbox SET status = 'cancelled' WHERE key = ? AND status = 'pending'", (key,))

    def retry_ticket(self, ticket_id: str) -> None:
        """The owner said go: anything uncertain or backing off runs again now."""
        self.store.conn.execute(
            "UPDATE outbox SET status = 'pending', next_at = ? WHERE ticket_id = ? AND status IN ('pending', 'uncertain')",
            (self.clock(), ticket_id),
        )

    def due(self, limit: int = 100) -> list[EffectRow]:
        rows = self.store.conn.execute(
            "SELECT * FROM outbox WHERE status = 'pending' AND next_at <= ? ORDER BY id LIMIT ?", (self.clock(), limit)
        ).fetchall()
        return [EffectRow(r["id"], r["key"], r["kind"], r["ticket_id"], json.loads(r["payload"]), r["attempts"]) for r in rows]

    def pump(
        self,
        handlers: Mapping[str, Handler],
        completions: Mapping[str, Completion] | None = None,
        on_uncertain: Uncertain | None = None,
    ) -> int:
        """Run what's due, outside any transaction. Returns how many rows were tried. Only an
        Exception counts as a failed attempt: a process crash leaves the row pending."""
        completions = completions or {}
        tried = 0
        for row in self.due():
            current = self.store.conn.execute("SELECT status FROM outbox WHERE id = ?", (row.id,)).fetchone()
            if current["status"] != "pending":
                continue  # an earlier effect in this batch cancelled it
            tried += 1
            try:
                result = handlers[row.kind](row)
            except UnknownOutcome as exc:
                with self.store.tx():
                    self.store.conn.execute(
                        "UPDATE outbox SET status = 'uncertain', last_error = ?, attempts = attempts + 1 WHERE id = ?",
                        (str(exc), row.id),
                    )
                    if on_uncertain:
                        on_uncertain(row, str(exc))
            except Exception as exc:
                attempts = row.attempts + 1
                with self.store.tx():
                    self.store.conn.execute(
                        "UPDATE outbox SET attempts = ?, next_at = ?, last_error = ? WHERE id = ?",
                        (attempts, self.clock() + backoff(attempts), f"{type(exc).__name__}: {exc}"[:500], row.id),
                    )
            else:
                with self.store.tx():
                    self.store.conn.execute(
                        "UPDATE outbox SET status = 'done', done_at = ? WHERE id = ?", (self.clock(), row.id)
                    )
                    completion = completions.get(row.kind)
                    if completion:
                        completion(row, result)
        return tried

    def counts(self) -> dict[str, int]:
        rows = self.store.conn.execute("SELECT status, COUNT(*) AS n FROM outbox GROUP BY status")
        out = {r["status"]: r["n"] for r in rows}
        failing = self.store.conn.execute("SELECT COUNT(*) FROM outbox WHERE status = 'pending' AND attempts >= 3").fetchone()[0]
        out["failing"] = failing
        return out

    def rows(self, kind: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM outbox" + (" WHERE kind = ?" if kind else "") + " ORDER BY id"
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in self.store.conn.execute(sql, (kind,) if kind else ())]
