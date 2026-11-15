"""§11.4: the store, its migrations, one writer; and the Outbox on its own."""

import os
import shutil
import sqlite3
import tempfile
import unittest

from lumbridge_factory.effects import Outbox, UnknownOutcome, backoff
from lumbridge_factory.fakes import FakeClock
from lumbridge_factory.store import MIGRATIONS, Migration, Store, StoreError, WriterBusy

V2 = Migration(
    version=3,
    up=(
        "ALTER TABLE tickets ADD COLUMN priority INTEGER NOT NULL DEFAULT 0",
        "CREATE TABLE notes (id INTEGER PRIMARY KEY, text TEXT NOT NULL)",
    ),
    down=(
        "DROP TABLE notes",
        "ALTER TABLE tickets DROP COLUMN priority",
    ),
)
BROKEN = Migration(
    version=3,
    up=("CREATE TABLE half (id INTEGER)", "THIS IS NOT SQL"),
    down=("DROP TABLE half",),
)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lumbridge-factory-store-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "s.sqlite3")


class MigrationTest(Base):
    def _seed(self):
        store = Store.writer(self.path)
        with store.tx():
            store.conn.execute(
                "INSERT INTO tickets (id, status, phase, repo, body, updated_at) VALUES ('t-1', 'open', 'building', 'r', '{}', 0)"
            )
        store.close()

    def test_new_store_is_wal_at_latest(self):
        store = Store.writer(self.path)
        self.addCleanup(store.close)
        self.assertEqual(store.schema_version, 2)
        self.assertEqual(store.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(store.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_forward_keeps_data_and_backs_up_first(self):
        self._seed()
        store = Store.writer(self.path, MIGRATIONS + (V2,))
        self.addCleanup(store.close)
        self.assertEqual(store.schema_version, 3)
        row = store.conn.execute("SELECT id, priority FROM tickets").fetchone()
        self.assertEqual((row["id"], row["priority"]), ("t-1", 0))
        backup = sqlite3.connect(store.backup_path(3))
        self.addCleanup(backup.close)
        self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(backup.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 1)

    def test_one_step_rollback_keeps_data_and_the_old_release_can_open_it(self):
        self._seed()
        store = Store.writer(self.path, MIGRATIONS + (V2,))
        with store.tx():
            store.conn.execute(
                "INSERT INTO tickets (id, status, phase, repo, body, updated_at, priority) VALUES ('t-2', 'open', 'building', 'r', '{}', 0, 5)"
            )
        self.assertEqual(store.rollback_one(), 2)
        self.assertEqual(store.schema_version, 2)
        store.close()

        old = Store.writer(self.path)  # the previous release
        self.addCleanup(old.close)
        self.assertEqual(old.schema_version, 2)
        rows = [r["id"] for r in old.conn.execute("SELECT id FROM tickets ORDER BY id")]
        self.assertEqual(rows, ["t-1", "t-2"], "written after the upgrade, still there")
        with self.assertRaises(sqlite3.OperationalError):
            old.conn.execute("SELECT * FROM notes")

    def test_upgrade_again_after_rollback(self):
        self._seed()
        store = Store.writer(self.path, MIGRATIONS + (V2,))
        store.rollback_one()
        store.close()
        again = Store.writer(self.path, MIGRATIONS + (V2,))
        self.addCleanup(again.close)
        self.assertEqual(again.schema_version, 3)

    def test_only_one_step_back(self):
        self._seed()
        store = Store.writer(self.path, MIGRATIONS + (V2,))
        self.addCleanup(store.close)
        store.rollback_one()
        with self.assertRaises(StoreError):
            store.rollback_one()

    def test_only_one_step_back_even_when_older_steps_have_rollbacks(self):
        v3 = Migration(version=4, up=("CREATE TABLE third (id INTEGER)",), down=("DROP TABLE third",))
        store = Store.writer(self.path, MIGRATIONS + (V2, v3))
        self.addCleanup(store.close)
        self.assertEqual(store.rollback_one(), 3)
        with self.assertRaises(StoreError):
            store.rollback_one()
        self.assertEqual(store.schema_version, 3)

    def test_baseline_has_no_rollback(self):
        store = Store.writer(self.path, MIGRATIONS[:1])
        self.addCleanup(store.close)
        with self.assertRaises(StoreError):
            store.rollback_one()

    def test_a_failed_migration_leaves_the_store_as_it_was(self):
        self._seed()
        with self.assertRaises(sqlite3.Error):
            Store.writer(self.path, MIGRATIONS + (BROKEN,))
        store = Store.writer(self.path)  # the lock came back, and the old release still opens it
        self.addCleanup(store.close)
        self.assertEqual(store.schema_version, 2)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 1)
        with self.assertRaises(sqlite3.OperationalError):
            store.conn.execute("SELECT * FROM half")

    def test_old_release_refuses_a_newer_store(self):
        self._seed()
        Store.writer(self.path, MIGRATIONS + (V2,)).close()
        with self.assertRaises(StoreError) as ctx:
            Store.writer(self.path)
        self.assertIn("roll the store back", str(ctx.exception))

    def test_a_gap_in_migrations_is_refused(self):
        with self.assertRaises(StoreError):
            Store.writer(self.path, MIGRATIONS + (Migration(version=5, up=("SELECT 1",)),))


class OneWriterTest(Base):
    def test_second_writer_is_refused_and_released_on_close(self):
        first = Store.writer(self.path)
        with self.assertRaises(WriterBusy):
            Store.writer(self.path)
        first.close()
        Store.writer(self.path).close()

    def test_reader_works_during_a_write_and_cant_write(self):
        writer = Store.writer(self.path)
        self.addCleanup(writer.close)
        reader = Store.reader(self.path)
        self.addCleanup(reader.close)
        with writer.tx():
            writer.conn.execute("INSERT INTO source_cursors VALUES ('a', '1', 0)")
            self.assertIsNone(reader.cursor("a"), "readers see the last commit")
        self.assertEqual(reader.cursor("a"), "1")
        with self.assertRaises(sqlite3.OperationalError):
            reader.conn.execute("INSERT INTO source_cursors VALUES ('b', '1', 0)")

    def test_reader_refuses_a_missing_store(self):
        with self.assertRaises(StoreError):
            Store.reader(self.path)

    def test_transactions_roll_back_whole_and_dont_nest(self):
        store = Store.writer(self.path)
        self.addCleanup(store.close)
        with self.assertRaises(ValueError):
            with store.tx():
                store.set_cursor("a", "1", 0)
                raise ValueError
        self.assertIsNone(store.cursor("a"))
        with store.tx():
            with self.assertRaises(StoreError):
                with store.tx():
                    pass


class OutboxTest(Base):
    def setUp(self):
        super().setUp()
        self.store = Store.writer(self.path)
        self.addCleanup(self.store.close)
        self.clock = FakeClock()
        self.outbox = Outbox(self.store, self.clock)

    def _enqueue(self, key="k", kind="x"):
        with self.store.tx():
            return self.outbox.enqueue(kind, key, {"a": 1})

    def test_same_key_is_one_row(self):
        self.assertTrue(self._enqueue())
        self.assertFalse(self._enqueue())
        self.assertEqual(len(self.outbox.rows()), 1)

    def test_an_effect_enqueued_in_a_rolled_back_transaction_never_happened(self):
        with self.assertRaises(ValueError):
            with self.store.tx():
                self.outbox.enqueue("x", "k", {})
                raise ValueError
        self.assertEqual(self.outbox.rows(), [])

    def test_done_runs_once(self):
        self._enqueue()
        calls = []
        self.assertEqual(self.outbox.pump({"x": calls.append}), 1)
        self.assertEqual(self.outbox.pump({"x": calls.append}), 0)
        self.assertEqual(len(calls), 1)

    def test_failure_doubles_to_five_minutes(self):
        self.assertEqual([backoff(n) for n in (1, 2, 3, 4)], [2.0, 4.0, 8.0, 16.0])
        self.assertEqual(backoff(50), 300.0)
        self.assertEqual(backoff(5000), 300.0)  # no OverflowError after days of failing
        self._enqueue()

        def fail(_):
            raise ConnectionError("down")

        self.outbox.pump({"x": fail})
        row = self.outbox.rows()[0]
        self.assertEqual((row["status"], row["attempts"]), ("pending", 1))
        self.assertIn("ConnectionError", row["last_error"])
        self.assertEqual(self.outbox.pump({"x": fail}), 0, "not due yet")
        self.clock.advance(2)
        self.assertEqual(self.outbox.pump({"x": fail}), 1)
        self.assertEqual(self.outbox.rows()[0]["attempts"], 2)

    def test_failing_shows_after_three(self):
        self._enqueue()

        def fail(_):
            raise ConnectionError

        for _ in range(3):
            self.clock.advance(100)
            self.outbox.pump({"x": fail})
        self.assertEqual(self.outbox.counts()["failing"], 1)

    def test_a_process_crash_isnt_a_failed_attempt(self):
        self._enqueue()

        class Crash(BaseException):
            pass

        def crash(_):
            raise Crash

        with self.assertRaises(Crash):
            self.outbox.pump({"x": crash})
        row = self.outbox.rows()[0]
        self.assertEqual((row["status"], row["attempts"]), ("pending", 0))

    def test_unknown_outcome_goes_uncertain_and_calls_back(self):
        self._enqueue()
        seen = []

        def unknown(_):
            raise UnknownOutcome("who knows")

        self.outbox.pump({"x": unknown}, on_uncertain=lambda row, what: seen.append(what))
        self.assertEqual(self.outbox.rows()[0]["status"], "uncertain")
        self.assertEqual(seen, ["who knows"])
        self.assertEqual(self.outbox.pump({"x": unknown}), 0, "doesn't retry by itself")

    def test_completion_shares_the_transaction_marking_done(self):
        self._enqueue()

        def bad(row, result):
            self.store.set_cursor("a", "1", 0)
            raise RuntimeError("after the effect")

        with self.assertRaises(RuntimeError):
            self.outbox.pump({"x": lambda r: 1}, {"x": bad})
        self.assertEqual(self.outbox.rows()[0]["status"], "pending")
        self.assertIsNone(self.store.cursor("a"))
