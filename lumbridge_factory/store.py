"""SQLite in WAL mode: the schema, its migrations, and the repositories (§11.1, §11.4).

One writer. `Store.writer()` takes an exclusive lock beside the database and fails if someone
else holds it; `Store.reader()` opens read-only. All writes happen inside `with store.tx():`.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any, Iterator, Sequence

from .core.executor import Role, WorktreeSpec
from .core.intake import SubRef
from .core.model import (
    ActorKind,
    Assignment,
    EndReason,
    Mandate,
    Phase,
    Strength,
    SubMode,
    Ticket,
    TicketStatus,
    Trigger,
    TriggerKind,
    WaitReason,
)
from .core.policy import Policy


class StoreError(Exception):
    pass


class WriterBusy(StoreError):
    """Another process holds the writer lock (normally factoryd)."""


@dataclass(frozen=True)
class Migration:
    version: int
    up: tuple[str, ...]
    down: tuple[str, ...] = ()


V1_UP = (
    """CREATE TABLE policy_versions (
        version INTEGER PRIMARY KEY, body TEXT NOT NULL, created_by TEXT NOT NULL, created_at REAL NOT NULL)""",
    """CREATE TABLE source_cursors (source TEXT PRIMARY KEY, cursor TEXT NOT NULL, updated_at REAL NOT NULL)""",
    """CREATE TABLE threads (
        key TEXT PRIMARY KEY, repo TEXT NOT NULL, created_at REAL NOT NULL, next_assess_at REAL NOT NULL DEFAULT 0)""",
    """CREATE TABLE triggers (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, source TEXT NOT NULL, external_id TEXT NOT NULL,
        thread TEXT NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL, actor_kind TEXT NOT NULL, body TEXT NOT NULL,
        repo TEXT NOT NULL, at REAL NOT NULL, data TEXT NOT NULL, UNIQUE (source, external_id))""",
    """CREATE TRIGGER triggers_immutable BEFORE UPDATE ON triggers
        BEGIN SELECT RAISE(ABORT, 'triggers are immutable'); END""",
    """CREATE TRIGGER triggers_no_delete BEFORE DELETE ON triggers
        BEGIN SELECT RAISE(ABORT, 'triggers are immutable'); END""",
    """CREATE TABLE trigger_routing (
        trigger_id TEXT PRIMARY KEY REFERENCES triggers(id), thread TEXT NOT NULL, status TEXT NOT NULL,
        hint TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '', eval_id TEXT, ticket_id TEXT)""",
    """CREATE INDEX trigger_routing_pending ON trigger_routing (thread) WHERE status = 'pending'""",
    """CREATE TABLE evals (
        id TEXT PRIMARY KEY, thread TEXT NOT NULL, status TEXT NOT NULL, rule TEXT NOT NULL, mode TEXT NOT NULL,
        strength TEXT NOT NULL, decision TEXT, would_accept INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
        ticket_id TEXT, trigger_ids TEXT NOT NULL, created_at REAL NOT NULL, done_at REAL)""",
    """CREATE UNIQUE INDEX one_running_eval ON evals (thread) WHERE status = 'running'""",
    """CREATE TABLE mandates (
        id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL UNIQUE, policy_version INTEGER NOT NULL, repo TEXT NOT NULL,
        base TEXT NOT NULL, origin_thread TEXT NOT NULL, strength TEXT NOT NULL, rules TEXT NOT NULL,
        requested_by TEXT NOT NULL, trigger_id TEXT NOT NULL, budget_tokens INTEGER NOT NULL,
        budget_wall_seconds INTEGER NOT NULL, created_at REAL NOT NULL)""",
    """CREATE TRIGGER mandates_immutable BEFORE UPDATE ON mandates
        BEGIN SELECT RAISE(ABORT, 'mandates are immutable'); END""",
    """CREATE TABLE tickets (
        id TEXT PRIMARY KEY, status TEXT NOT NULL, phase TEXT NOT NULL, repo TEXT NOT NULL, body TEXT NOT NULL,
        updated_at REAL NOT NULL)""",
    """CREATE TABLE assignments (
        id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id), role TEXT NOT NULL, live INTEGER NOT NULL,
        session_id TEXT, native_session_id TEXT, account TEXT NOT NULL, model TEXT NOT NULL, worktree TEXT NOT NULL,
        created_at REAL NOT NULL)""",
    """CREATE TABLE subscriptions (
        ticket_id TEXT NOT NULL REFERENCES tickets(id), thread TEXT NOT NULL, mode TEXT NOT NULL,
        PRIMARY KEY (ticket_id, thread))""",
    """CREATE INDEX subscriptions_thread ON subscriptions (thread)""",
    """CREATE TABLE inputs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, assignment_id TEXT NOT NULL REFERENCES assignments(id),
        ticket_id TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, ref TEXT NOT NULL, wake INTEGER NOT NULL,
        urgent INTEGER NOT NULL, turn_id TEXT, created_at REAL NOT NULL)""",
    """CREATE INDEX inputs_pending ON inputs (assignment_id) WHERE turn_id IS NULL""",
    """CREATE TABLE turns (
        id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, assignment_id TEXT NOT NULL, status TEXT NOT NULL,
        slot TEXT NOT NULL, request TEXT NOT NULL, outcome TEXT, tokens INTEGER NOT NULL DEFAULT 0,
        wall_seconds REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', started_at REAL NOT NULL,
        last_event_at REAL NOT NULL, ended_at REAL)""",
    """CREATE UNIQUE INDEX one_turn_per_assignment ON turns (assignment_id) WHERE status IN ('launching', 'running')""",
    """CREATE TABLE entries (
        turn_id TEXT NOT NULL, seq INTEGER NOT NULL, ticket_id TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
        at REAL NOT NULL, PRIMARY KEY (turn_id, seq))""",
    """CREATE TABLE outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, ticket_id TEXT,
        payload TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL,
        last_error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, done_at REAL)""",
    """CREATE INDEX outbox_due ON outbox (next_at) WHERE status = 'pending'""",
)

V2_UP = (
    """CREATE TABLE feed (
        id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE, thread TEXT NOT NULL, text TEXT NOT NULL,
        at REAL NOT NULL)""",
)
V2_DOWN = ("DROP TABLE feed",)

MIGRATIONS: tuple[Migration, ...] = (Migration(version=1, up=V1_UP), Migration(version=2, up=V2_UP, down=V2_DOWN))


def _ticket_to_json(t: Ticket) -> str:
    return json.dumps(asdict(t), sort_keys=True)


def _ticket_from_json(text: str) -> Ticket:
    d = json.loads(text)
    d["status"] = TicketStatus(d["status"])
    d["phase"] = Phase(d["phase"])
    d["end_reason"] = None if d["end_reason"] is None else EndReason(d["end_reason"])
    d["waiting_reason"] = None if d["waiting_reason"] is None else WaitReason(d["waiting_reason"])
    return Ticket(**d)


class Store:
    def __init__(self, path: str, conn: sqlite3.Connection, migrations: Sequence[Migration], lock_fd: int | None):
        self.path = path
        self.conn = conn
        self.migrations = tuple(migrations)
        self._lock_fd = lock_fd
        self._in_tx = False

    # --- opening ----------------------------------------------------------------

    @classmethod
    def writer(cls, path: str, migrations: Sequence[Migration] = MIGRATIONS, migrate: bool = True) -> "Store":
        lock_fd = os.open(path + ".writer", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(lock_fd)
            raise WriterBusy(f"another process is the writer of {path}") from None
        conn = sqlite3.connect(path, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        store = cls(path, conn, migrations, lock_fd)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            if migrate:
                store.migrate()
            else:
                store._refuse_newer()
        except BaseException:
            store.close()  # or the writer lock stays held by this process
            raise
        return store

    @classmethod
    def reader(cls, path: str, migrations: Sequence[Migration] = MIGRATIONS) -> "Store":
        if not os.path.exists(path):
            raise StoreError(f"no store at {path}")
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        store = cls(path, conn, migrations, None)
        store._refuse_newer()
        return store

    def close(self) -> None:
        self.conn.close()
        if self._lock_fd is not None:
            os.close(self._lock_fd)  # closing the descriptor releases the flock
            self._lock_fd = None

    # --- migrations --------------------------------------------------------------

    @property
    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    @property
    def latest_version(self) -> int:
        return max((m.version for m in self.migrations), default=0)

    def _refuse_newer(self) -> None:
        if self.schema_version > self.latest_version:
            raise StoreError(
                f"store is at schema {self.schema_version}, this release knows {self.latest_version}: "
                "roll the store back first, or run the newer release"
            )

    def backup_path(self, version: int) -> str:
        return f"{self.path}.pre-v{version}"

    def backup(self, target: str) -> None:
        dest = sqlite3.connect(target)
        try:
            self.conn.backup(dest)
        finally:
            dest.close()

    def migrate(self) -> list[int]:
        """Forward to the latest schema, one transaction per step, with a backup before each
        step on a store that already has data."""
        self._refuse_newer()
        applied = []
        for migration in sorted(self.migrations, key=lambda m: m.version):
            current = self.schema_version
            if migration.version <= current:
                continue
            if migration.version != current + 1:
                raise StoreError(f"no migration from {current} to {migration.version}")
            if current > 0:
                self.backup(self.backup_path(migration.version))
            self._run_step(migration.up, migration.version)
            applied.append(migration.version)
        return applied

    def rollback_one(self) -> int:
        """Back one schema step, from the latest to the one before it. The supported rollback
        (§11.4): the previous release runs against the result."""
        current = self.schema_version
        if current != self.latest_version:
            raise StoreError(f"only one step back from {self.latest_version} is supported; store is at {current}")
        migration = next(m for m in self.migrations if m.version == current)
        if not migration.down:
            raise StoreError(f"schema {current} has no rollback")
        self._run_step(migration.down, current - 1)
        return current - 1

    def _run_step(self, statements: Sequence[str], version: int) -> None:
        # Rebuilding a table during a step must not cascade.
        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            with self.tx():
                for statement in statements:
                    self.conn.execute(statement)
                self.conn.execute(f"PRAGMA user_version = {int(version)}")
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")

    # --- transactions ------------------------------------------------------------

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        if self._in_tx:
            raise StoreError("transactions don't nest")
        self.conn.execute("BEGIN IMMEDIATE")
        self._in_tx = True
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")
        finally:
            self._in_tx = False

    # --- policy ------------------------------------------------------------------

    def policy(self) -> Policy | None:
        row = self.conn.execute("SELECT body FROM policy_versions ORDER BY version DESC LIMIT 1").fetchone()
        return None if row is None else Policy.from_dict(json.loads(row["body"]))

    def add_policy(self, policy: Policy, created_by: str, now: float) -> None:
        self.conn.execute(
            "INSERT INTO policy_versions (version, body, created_by, created_at) VALUES (?, ?, ?, ?)",
            (policy.version, json.dumps(policy.to_dict(), sort_keys=True), created_by, now),
        )

    def policy_history(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT version, created_by, created_at FROM policy_versions ORDER BY version")
        return [dict(r) for r in rows]

    # --- sources -----------------------------------------------------------------

    def cursor(self, source: str) -> str | None:
        row = self.conn.execute("SELECT cursor FROM source_cursors WHERE source = ?", (source,)).fetchone()
        return None if row is None else row["cursor"]

    def set_cursor(self, source: str, cursor: str, now: float) -> None:
        self.conn.execute(
            "INSERT INTO source_cursors (source, cursor, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (source) DO UPDATE SET cursor = excluded.cursor, updated_at = excluded.updated_at",
            (source, cursor, now),
        )

    # --- triggers and threads ------------------------------------------------------

    def add_trigger(self, t: Trigger, now: float, status: str = "pending", ticket_id: str | None = None) -> bool:
        """Stored once. False if this (source, external_id) was already seen."""
        self.conn.execute(
            "INSERT OR IGNORE INTO threads (key, repo, created_at) VALUES (?, ?, ?)", (t.thread, t.repo, now)
        )
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO triggers (id, source, external_id, thread, kind, actor, actor_kind, body, repo, at, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (t.id, t.source, t.external_id, t.thread, t.kind, t.actor, t.actor_kind, t.body, t.repo, t.at or now,
             json.dumps(t.data, sort_keys=True)),
        )
        if cur.rowcount == 0:
            return False
        self.conn.execute(
            "INSERT INTO trigger_routing (trigger_id, thread, status, ticket_id) VALUES (?, ?, ?, ?)",
            (t.id, t.thread, status, ticket_id),
        )
        return True

    @staticmethod
    def _trigger(row: sqlite3.Row) -> Trigger:
        return Trigger(
            source=row["source"],
            external_id=row["external_id"],
            thread=row["thread"],
            kind=TriggerKind(row["kind"]),
            actor=row["actor"],
            actor_kind=ActorKind(row["actor_kind"]),
            body=row["body"],
            repo=row["repo"],
            at=row["at"],
            data=json.loads(row["data"]),
        )

    def trigger(self, trigger_id: str) -> Trigger | None:
        row = self.conn.execute("SELECT * FROM triggers WHERE id = ?", (trigger_id,)).fetchone()
        return None if row is None else self._trigger(row)

    def pending_threads(self) -> list[str]:
        rows = self.conn.execute("SELECT DISTINCT thread FROM trigger_routing WHERE status = 'pending'")
        return [r["thread"] for r in rows]

    def pending_triggers(self, thread: str) -> list[tuple[Trigger, str]]:
        rows = self.conn.execute(
            "SELECT t.*, r.hint AS hint FROM trigger_routing r JOIN triggers t ON t.id = r.trigger_id "
            "WHERE r.thread = ? AND r.status = 'pending' ORDER BY t.seq",
            (thread,),
        )
        return [(self._trigger(r), r["hint"]) for r in rows]

    def route_trigger(self, trigger_id: str, status: str, reason: str = "", eval_id: str | None = None, ticket_id: str | None = None) -> None:
        self.conn.execute(
            "UPDATE trigger_routing SET status = ?, reason = ?, eval_id = ?, ticket_id = COALESCE(?, ticket_id), hint = '' "
            "WHERE trigger_id = ?",
            (status, reason, eval_id, ticket_id, trigger_id),
        )

    def repend_trigger(self, trigger_id: str, hint: str = "") -> None:
        self.conn.execute(
            "UPDATE trigger_routing SET status = 'pending', eval_id = NULL, hint = ? WHERE trigger_id = ?",
            (hint, trigger_id),
        )

    def routing(self, trigger_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM trigger_routing WHERE trigger_id = ?", (trigger_id,)).fetchone()
        return None if row is None else dict(row)

    def next_assess_at(self, thread: str) -> float:
        row = self.conn.execute("SELECT next_assess_at FROM threads WHERE key = ?", (thread,)).fetchone()
        return 0.0 if row is None else row["next_assess_at"]

    def set_next_assess_at(self, thread: str, at: float) -> None:
        self.conn.execute("UPDATE threads SET next_assess_at = ? WHERE key = ?", (at, thread))

    # --- evals -------------------------------------------------------------------

    def add_eval(self, id: str, thread: str, status: str, rule: str, mode: str, strength: str, trigger_ids: Sequence[str], now: float, decision: str | None = None, ticket_id: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO evals (id, thread, status, rule, mode, strength, decision, ticket_id, trigger_ids, created_at, done_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (id, thread, status, rule, mode, strength, decision, ticket_id, json.dumps(list(trigger_ids)), now,
             now if status == "done" else None),
        )

    def finish_eval(self, id: str, decision: str, would_accept: bool, note: str, ticket_id: str | None, now: float) -> None:
        self.conn.execute(
            "UPDATE evals SET status = 'done', decision = ?, would_accept = ?, note = ?, ticket_id = ?, done_at = ? WHERE id = ?",
            (decision, int(would_accept), note, ticket_id, now, id),
        )

    def eval(self, id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM evals WHERE id = ?", (id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["trigger_ids"] = json.loads(d["trigger_ids"])
        return d

    def evals(self, rule: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id FROM evals" + (" WHERE rule = ?" if rule else "") + " ORDER BY rowid"
        return [self.eval(r["id"]) for r in self.conn.execute(sql, (rule,) if rule else ())]

    def running_eval(self, thread: str) -> bool:
        q = "SELECT 1 FROM evals WHERE thread = ? AND status = 'running'"
        return self.conn.execute(q, (thread,)).fetchone() is not None

    def count_evals(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM evals").fetchone()[0]

    # --- mandates ----------------------------------------------------------------

    def add_mandate(self, m: Mandate) -> None:
        self.conn.execute(
            "INSERT INTO mandates (id, ticket_id, policy_version, repo, base, origin_thread, strength, rules, requested_by, "
            "trigger_id, budget_tokens, budget_wall_seconds, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (m.id, m.ticket_id, m.policy_version, m.repo, m.base, m.origin_thread, m.strength, json.dumps(list(m.rules)),
             m.requested_by, m.trigger_id, m.budget_tokens, m.budget_wall_seconds, m.created_at),
        )

    def mandate(self, id: str) -> Mandate | None:
        row = self.conn.execute("SELECT * FROM mandates WHERE id = ?", (id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["strength"] = Strength(d["strength"])
        d["rules"] = tuple(json.loads(d["rules"]))
        return Mandate(**d)

    # --- tickets -----------------------------------------------------------------

    def save_ticket(self, t: Ticket, now: float) -> None:
        self.conn.execute(
            "INSERT INTO tickets (id, status, phase, repo, body, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET status = excluded.status, phase = excluded.phase, body = excluded.body, "
            "updated_at = excluded.updated_at",
            (t.id, t.status, t.phase, t.repo, _ticket_to_json(t), now),
        )

    def ticket(self, id: str) -> Ticket | None:
        row = self.conn.execute("SELECT body FROM tickets WHERE id = ?", (id,)).fetchone()
        return None if row is None else _ticket_from_json(row["body"])

    def tickets(self, status: str | None = None) -> list[Ticket]:
        if status:
            rows = self.conn.execute("SELECT body FROM tickets WHERE status = ? ORDER BY rowid", (status,))
        else:
            rows = self.conn.execute("SELECT body FROM tickets ORDER BY rowid")
        return [_ticket_from_json(r["body"]) for r in rows]

    # --- assignments -------------------------------------------------------------

    def add_assignment(self, a: Assignment) -> None:
        self.conn.execute(
            "INSERT INTO assignments (id, ticket_id, role, live, session_id, native_session_id, account, model, worktree, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (a.id, a.ticket_id, a.role, int(a.live), a.session_id, a.native_session_id, a.account, a.model,
             json.dumps(asdict(a.worktree)), a.created_at),
        )

    @staticmethod
    def _assignment(row: sqlite3.Row) -> Assignment:
        return Assignment(
            id=row["id"],
            ticket_id=row["ticket_id"],
            role=Role(row["role"]),
            worktree=WorktreeSpec(**json.loads(row["worktree"])),
            model=row["model"],
            account=row["account"],
            session_id=row["session_id"],
            native_session_id=row["native_session_id"],
            live=bool(row["live"]),
            created_at=row["created_at"],
        )

    def assignment(self, id: str) -> Assignment | None:
        row = self.conn.execute("SELECT * FROM assignments WHERE id = ?", (id,)).fetchone()
        return None if row is None else self._assignment(row)

    def assignments(self, ticket_id: str, live_only: bool = False) -> list[Assignment]:
        sql = "SELECT * FROM assignments WHERE ticket_id = ?" + (" AND live = 1" if live_only else "") + " ORDER BY rowid"
        return [self._assignment(r) for r in self.conn.execute(sql, (ticket_id,))]

    def live_builder(self, ticket_id: str) -> Assignment | None:
        row = self.conn.execute(
            "SELECT * FROM assignments WHERE ticket_id = ? AND role = 'builder' AND live = 1 ORDER BY rowid DESC LIMIT 1",
            (ticket_id,),
        ).fetchone()
        return None if row is None else self._assignment(row)

    def update_assignment(self, id: str, **fields: Any) -> None:
        allowed = {"live", "session_id", "native_session_id", "account"}
        if not set(fields) <= allowed:
            raise StoreError(f"can't update {set(fields) - allowed}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE assignments SET {sets} WHERE id = ?", (*fields.values(), id))

    def count_assignments(self, ticket_id: str, role: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM assignments WHERE ticket_id = ?"
        args: tuple = (ticket_id,)
        if role:
            sql += " AND role = ?"
            args += (role,)
        return self.conn.execute(sql, args).fetchone()[0]

    # --- subscriptions -----------------------------------------------------------

    def subscribe(self, ticket_id: str, thread: str, mode: SubMode) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO subscriptions (ticket_id, thread, mode) VALUES (?, ?, ?)", (ticket_id, thread, mode)
        )

    def unsubscribe(self, ticket_id: str, thread: str) -> None:
        self.conn.execute("DELETE FROM subscriptions WHERE ticket_id = ? AND thread = ?", (ticket_id, thread))

    def subs_of_thread(self, thread: str) -> list[SubRef]:
        rows = self.conn.execute(
            "SELECT s.ticket_id, s.mode, t.body FROM subscriptions s JOIN tickets t ON t.id = s.ticket_id "
            "WHERE s.thread = ? ORDER BY s.rowid",
            (thread,),
        )
        refs = []
        for r in rows:
            body = json.loads(r["body"])
            refs.append(SubRef(r["ticket_id"], SubMode(r["mode"]), body["stopped"], body["status"] == TicketStatus.ENDED))
        return refs

    def subs_of_ticket(self, ticket_id: str) -> list[tuple[str, SubMode]]:
        q = "SELECT thread, mode FROM subscriptions WHERE ticket_id = ? ORDER BY rowid"
        return [(r["thread"], SubMode(r["mode"])) for r in self.conn.execute(q, (ticket_id,))]

    # --- inputs ------------------------------------------------------------------

    def add_input(self, assignment_id: str, ticket_id: str, kind: str, text: str, ref: str, wake: bool, urgent: bool, now: float) -> None:
        self.conn.execute(
            "INSERT INTO inputs (assignment_id, ticket_id, kind, text, ref, wake, urgent, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (assignment_id, ticket_id, kind, text, ref, int(wake), int(urgent), now),
        )

    def pending_inputs(self, assignment_id: str) -> list[sqlite3.Row]:
        # The assignment text is always first, or a replacement session would read it last.
        return self.conn.execute(
            "SELECT * FROM inputs WHERE assignment_id = ? AND turn_id IS NULL ORDER BY (kind != 'assignment'), id",
            (assignment_id,),
        ).fetchall()

    def bind_inputs(self, ids: Sequence[int], turn_id: str) -> None:
        self.conn.executemany("UPDATE inputs SET turn_id = ? WHERE id = ?", [(turn_id, i) for i in ids])

    def requeue_inputs(self, turn_id: str) -> None:
        self.conn.execute("UPDATE inputs SET turn_id = NULL WHERE turn_id = ?", (turn_id,))

    def drop_recovery_notes(self, assignment_id: str) -> None:
        self.conn.execute(
            "DELETE FROM inputs WHERE assignment_id = ? AND turn_id IS NULL AND kind = 'recovery'", (assignment_id,)
        )

    def drop_pending_inputs(self, ticket_id: str, system_only: bool = False) -> None:
        sql = "DELETE FROM inputs WHERE ticket_id = ? AND turn_id IS NULL"
        self.conn.execute(sql + (" AND urgent = 0" if system_only else ""), (ticket_id,))

    def move_pending_inputs(self, old: str, new: str) -> None:
        self.conn.execute("UPDATE inputs SET assignment_id = ? WHERE assignment_id = ? AND turn_id IS NULL", (new, old))

    def first_assignment_input(self, assignment_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM inputs WHERE assignment_id = ? AND kind = 'assignment' ORDER BY id LIMIT 1", (assignment_id,)
        ).fetchone()

    def delivered_ref(self, ticket_id: str, ref: str) -> bool:
        q = "SELECT 1 FROM inputs WHERE ticket_id = ? AND ref = ? AND ref != ''"
        return self.conn.execute(q, (ticket_id, ref)).fetchone() is not None

    def delivered_refs(self, ticket_id: str) -> list[str]:
        q = "SELECT ref FROM inputs WHERE ticket_id = ? AND ref != '' AND kind = 'trigger' GROUP BY ref ORDER BY MIN(id)"
        return [r["ref"] for r in self.conn.execute(q, (ticket_id,))]

    def candidates(self) -> list[sqlite3.Row]:
        """Live Assignments with something waking them and no turn in flight."""
        return self.conn.execute(
            """SELECT a.id AS assignment_id, a.ticket_id, a.role, a.session_id, a.native_session_id,
                      MIN(i.id) AS position, MAX(i.urgent) AS urgent
               FROM assignments a JOIN inputs i ON i.assignment_id = a.id AND i.turn_id IS NULL
               WHERE a.live = 1
                 AND NOT EXISTS (SELECT 1 FROM turns u WHERE u.assignment_id = a.id AND u.status IN ('launching', 'running'))
               GROUP BY a.id HAVING MAX(i.wake) = 1 ORDER BY position"""
        ).fetchall()

    # --- turns -------------------------------------------------------------------

    def add_turn(self, id: str, ticket_id: str, assignment_id: str, slot: str, request: dict, now: float) -> None:
        self.conn.execute(
            "INSERT INTO turns (id, ticket_id, assignment_id, status, slot, request, started_at, last_event_at) "
            "VALUES (?, ?, ?, 'launching', ?, ?, ?, ?)",
            (id, ticket_id, assignment_id, slot, json.dumps(request, sort_keys=True), now, now),
        )

    def turn(self, id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM turns WHERE id = ?", (id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["request"] = json.loads(d["request"])
        return d

    def turns(self, *, ticket_id: str | None = None, statuses: Sequence[str] = ()) -> list[dict[str, Any]]:
        clauses, args = [], []
        if ticket_id:
            clauses.append("ticket_id = ?")
            args.append(ticket_id)
        if statuses:
            clauses.append("status IN (" + ",".join("?" * len(statuses)) + ")")
            args.extend(statuses)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.conn.execute("SELECT id FROM turns" + where + " ORDER BY rowid", args)
        return [self.turn(r["id"]) for r in rows.fetchall()]

    def slots_used(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT slot, COUNT(*) AS n FROM turns WHERE status IN ('launching', 'running') GROUP BY slot"
        )
        return {r["slot"]: r["n"] for r in rows}

    def update_turn(self, id: str, **fields: Any) -> None:
        allowed = {"status", "outcome", "tokens", "wall_seconds", "error", "last_event_at", "ended_at"}
        if not set(fields) <= allowed:
            raise StoreError(f"can't update {set(fields) - allowed}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE turns SET {sets} WHERE id = ?", (*fields.values(), id))

    def count_turns(self, assignment_id: str) -> int:
        q = "SELECT COUNT(*) FROM turns WHERE assignment_id = ?"
        return self.conn.execute(q, (assignment_id,)).fetchone()[0]

    # --- the feed: what has no forge thread to go to (§4.4) ---------------------------

    def add_feed(self, key: str, thread: str, text: str, now: float) -> None:
        self.conn.execute("INSERT OR IGNORE INTO feed (key, thread, text, at) VALUES (?, ?, ?, ?)", (key, thread, text, now))

    def has_feed(self, key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM feed WHERE key = ?", (key,)).fetchone() is not None

    def feed(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT thread, text, at FROM feed ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # --- cleanup ---------------------------------------------------------------------

    def retired_sessions(self) -> list[Assignment]:
        """Sessions no one will use again whose removal hasn't been asked for."""
        rows = self.conn.execute(
            """SELECT a.* FROM assignments a
               WHERE a.live = 0 AND a.session_id IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.key = 'remove_session:' || a.id)"""
        )
        return [self._assignment(r) for r in rows.fetchall()]

    def ended_builders(self, before: float) -> list[Assignment]:
        """Builders of Tickets that ended before `before`, whose worktree is still there."""
        rows = self.conn.execute(
            """SELECT a.* FROM assignments a JOIN tickets t ON t.id = a.ticket_id
               WHERE a.live = 1 AND a.role = 'builder' AND a.session_id IS NOT NULL
                 AND t.status = 'ended' AND t.updated_at <= ?
                 AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.key = 'remove_session:' || a.id)""",
            (before,),
        )
        return [self._assignment(r) for r in rows.fetchall()]

    # --- entries (the worker's reports and tool calls) -----------------------------

    def add_entry(self, turn_id: str, seq: int, ticket_id: str, kind: str, body: dict, now: float) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO entries (turn_id, seq, ticket_id, kind, body, at) VALUES (?, ?, ?, ?, ?, ?)",
            (turn_id, seq, ticket_id, kind, json.dumps(body, sort_keys=True), now),
        )
        return cur.rowcount == 1

    def entries(self, *, turn_id: str | None = None, ticket_id: str | None = None) -> list[dict[str, Any]]:
        if turn_id:
            rows = self.conn.execute("SELECT * FROM entries WHERE turn_id = ? ORDER BY seq", (turn_id,))
        else:
            rows = self.conn.execute("SELECT * FROM entries WHERE ticket_id = ? ORDER BY at, turn_id, seq", (ticket_id,))
        return [{**dict(r), "body": json.loads(r["body"])} for r in rows]
