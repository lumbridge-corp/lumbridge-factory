"""§8: recover, don't quarantine. Crashes at every interesting moment."""

from lumbridge_factory.core.executor import ExecutorError, Role, TurnOutcome
from lumbridge_factory.core.facts import CiState
from lumbridge_factory.core.model import Phase, Trigger, TriggerKind, WaitReason, issue_thread
from lumbridge_factory.core.prompts import RECOVERY_NOTE
from lumbridge_factory.fakes import FakeSource, FakeTurn, ready, report
from lumbridge_factory.turns import ThreadRunner

from harness import REPO, WorldTest


class Crash(BaseException):
    """Stands in for the process dying: nothing after it runs."""


class RestartTest(WorldTest):
    def test_factoryd_restart_with_the_turn_still_alive_reattaches(self):
        w = self.world()
        id = w.issue()
        w.executor.script(f"{id}-b1", FakeTurn(hold=True, replies=("working on it",)))
        w.settle()
        w.restart()
        w.settle()
        self.assertEqual(len(w.builder_texts(id)), 1, "no second turn")
        self.assertEqual(w.comments().count("working on it"), 1, "replay isn't reposted")
        w.executor.script(f"{id}-b1", FakeTurn(reports=(report("blocked", question="q?"),)))
        w.executor.release(f"{id}-b1-1")
        w.settle()
        self.assertEqual(len(w.builder_texts(id)), 2)
        self.assertNotIn(RECOVERY_NOTE, w.builder_texts(id)[1])

    def test_turn_finished_while_factoryd_was_down_is_collected(self):
        w = self.world()
        id = w.issue()
        pr = w.forge.open_pr(REPO, f"factory/{id}")
        w.executor.script(f"{id}-b1", FakeTurn(hold=True, reports=(ready(pr.number),)))
        w.settle()
        w.executor.release(f"{id}-b1-1")
        w.restart()
        w.settle()
        self.assertEqual(w.ticket(id).phase, Phase.GATING)
        self.assertEqual(len(w.builder_texts(id)), 1)

    def test_turn_interrupted_midway_recovers_with_no_duplicate_comment(self):
        w = self.world()
        id = w.issue(body="the task")
        # The first turn posts a reply, then the host dies.
        w.executor.script(f"{id}-b1", FakeTurn(hold=True, replies=("Pushed the first part.",)))
        w.settle()
        self.assertEqual(w.comments().count("Pushed the first part."), 1)
        # The recovery turn, being careful, says the same thing again, and finishes.
        pr = w.forge.open_pr(REPO, f"factory/{id}", ci=CiState.GREEN)
        w.executor.script(f"{id}-b1", FakeTurn(replies=("Pushed the first part.",), reports=(ready(pr.number),)))
        w.executor.host_restart()
        w.restart()
        w.settle()

        self.assertEqual(w.comments().count("Pushed the first part."), 1, "no duplicate comment")
        self.assertEqual(sum("Factory is on it" in c for c in w.comments()), 1, "no second acknowledgement")
        requests = w.executor.requests_for(f"{id}-b1")
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1].session_id, requests[0].session_id, "same session")
        text = w.builder_texts(id)[1]
        self.assertIn(RECOVERY_NOTE, text)
        self.assertIn("the task", text, "what it may not have finished reading comes again")
        t = w.ticket(id)
        self.assertFalse(t.quarantined)
        self.assertEqual(t.phase, Phase.READY)

    def test_agentd_lost_its_journal_is_still_a_recovery(self):
        w = self.world()
        id = w.issue()
        w.executor.script(f"{id}-b1", FakeTurn(hold=True), FakeTurn(reports=(report("blocked", question="q?"),)))
        w.settle()
        w.executor.lose_journal()
        w.restart()
        w.settle()
        self.assertIn(RECOVERY_NOTE, w.builder_texts(id)[-1])
        self.assertFalse(w.ticket(id).quarantined)

    def test_executor_down_at_restart_waits_and_then_recovers(self):
        w = self.world()
        id = w.issue()
        w.executor.script(f"{id}-b1", FakeTurn(hold=True), FakeTurn(reports=(report("blocked", question="q?"),)))
        w.settle()
        w.executor.host_restart()
        w.executor.down = True
        w.restart()
        w.settle()
        self.assertEqual(w.store.turns(statuses=("running",))[0]["id"], f"{id}-b1-1", "still ours to resolve")
        w.executor.down = False
        w.settle()
        self.assertEqual(len(w.builder_texts(id)), 2)

    def test_recovery_inputs_dont_pile_up_notes(self):
        w = self.world()
        id = w.issue()
        w.executor.script(f"{id}-b1", FakeTurn(hold=True), FakeTurn(hold=True), FakeTurn(reports=(report("blocked", question="q?"),)))
        w.settle()
        w.executor.host_restart()
        w.restart()
        w.settle()
        w.executor.host_restart()
        w.restart()
        w.settle()
        self.assertEqual(w.builder_texts(id)[-1].count(RECOVERY_NOTE), 1)

    def test_repeated_cut_offs_ask_owner_instead_of_looping_or_quarantining(self):
        w = self.world()
        id = w.issue()
        for _ in range(10):
            w.executor.script(f"{id}-b1", FakeTurn(outcome=TurnOutcome.INTERRUPTED))
        w.settle()
        t = w.ticket(id)
        self.assertEqual(t.waiting_reason, WaitReason.TROUBLE)
        self.assertFalse(t.quarantined)
        self.assertEqual(len(w.builder_texts(id)), 6)

    def test_timeout_gets_a_note_and_a_new_turn(self):
        w = self.world()
        id = w.issue()
        w.executor.script(f"{id}-b1", FakeTurn(outcome=TurnOutcome.TIMEOUT), FakeTurn(reports=(report("blocked", question="q?"),)))
        w.settle()
        self.assertIn("wall-clock", w.builder_texts(id)[1])

    def test_transient_failures_back_off_then_ask(self):
        w = self.world()
        id = w.issue(body="the task")
        for _ in range(4):
            w.executor.script(f"{id}-b1", FakeTurn(outcome=TurnOutcome.TRANSIENT, error="HTTP 529"))
        w.settle()
        self.assertEqual(len(w.builder_texts(id)), 1, "waits out the backoff")
        for expected in (2, 3, 4):
            w.clock.advance(60)
            w.settle()
            self.assertEqual(len(w.builder_texts(id)), expected)
            self.assertIn("the task", w.builder_texts(id)[-1], "same inputs")
        t = w.ticket(id)
        self.assertEqual(t.waiting_reason, WaitReason.TROUBLE)
        self.assertIn("HTTP 529", t.question)

    def test_resume_failed_makes_a_new_assignment_in_the_same_worktree(self):
        w = self.world()
        id = w.issue(body="the task")
        w.executor.script(
            f"{id}-b1",
            FakeTurn(reports=(report("blocked", question="Which?"),)),
            FakeTurn(outcome=TurnOutcome.RESUME_FAILED),
        )
        w.executor.script(f"{id}-b2", FakeTurn(reports=(report("blocked", question="Now what?"),)))
        w.settle()
        w.comment(issue_thread(REPO, 1), "that one")
        w.settle()
        starts = [s for s in w.executor.starts if s.role == Role.BUILDER]
        self.assertEqual([s.assignment for s in starts], [f"{id}-b1", f"{id}-b2"])
        self.assertEqual(starts[0].worktree, starts[1].worktree)
        requests = w.executor.requests_for(f"{id}-b2")
        self.assertIsNone(requests[0].native_session_id, "a fresh session")
        text = w.builder_texts(id, 2)[0]
        self.assertTrue(text.startswith("Ticket "), "the assignment comes first")
        self.assertIn("the task", text)
        self.assertIn("that one", text)
        self.assertIn(RECOVERY_NOTE, text)

    def test_a_dropped_stream_with_the_turn_alive_reattaches(self):
        w = self.world()
        id = w.issue()
        w.executor.break_streams = 1
        w.executor.script(f"{id}-b1", FakeTurn(hold=True), FakeTurn(reports=(report("blocked", question="q?"),)))
        w.settle()
        self.assertEqual(w.executor.held(), [f"{id}-b1-1"])
        self.assertEqual(len(w.builder_texts(id)), 1, "no second turn")
        self.assertTrue(w.engine.runner.is_live(f"{id}-b1-1"))
        w.executor.release(f"{id}-b1-1")
        w.settle()
        self.assertNotIn(RECOVERY_NOTE, w.builder_texts(id)[1], "nothing was cut off")


class CrashAroundEffectsTest(WorldTest):
    def test_crash_between_commit_and_effect_runs_it_after_restart(self):
        w = self.world()
        id = w.issue()  # committed: Ticket + ack in the Outbox; nothing performed yet
        self.assertEqual(w.comments(), [])
        w.restart()
        w.executor.script(f"{id}-b1", FakeTurn(reports=(report("blocked", question="q?"),)))
        w.settle()
        self.assertEqual(sum("Factory is on it" in c for c in w.comments()), 1)

    def test_crash_right_after_the_post_doesnt_post_twice(self):
        w = self.world()
        id = w.issue()
        w.executor.script(f"{id}-b1", FakeTurn(reports=(report("blocked", question="q?"),)))
        w.forge.crash_after_post = Crash()
        with self.assertRaises(Crash):
            w.settle()
        self.assertEqual(sum("Factory is on it" in c for c in w.comments()), 1, "posted")
        row = w.engine.outbox.rows("comment")[0]
        self.assertEqual(row["status"], "pending", "bookkeeping never happened")
        w.restart()
        w.settle()
        self.assertEqual(sum("Factory is on it" in c for c in w.comments()), 1, "not again")

    def test_crash_after_starting_a_turn_doesnt_start_a_second(self):
        w = self.world()
        id = w.issue()
        w.executor.script(f"{id}-b1", FakeTurn(hold=True))
        w.engine.schedule()
        real = w.engine._completions["start_turn"]

        def boom(row, result):
            raise Crash()

        w.engine._completions["start_turn"] = boom
        with self.assertRaises(Crash):
            w.engine.pump()
        w.engine._completions["start_turn"] = real
        self.assertEqual(len(w.executor.requests), 1)
        w.restart()
        w.settle()
        self.assertEqual(len(w.executor.requests), 1, "turn() attached by turn_id")
        self.assertEqual(len(w.executor.starts), 1)
        self.assertEqual(w.store.turn(f"{id}-b1-1")["status"], "running")

    def test_retries_back_off_and_succeed_once(self):
        w = self.world()
        id = w.issue()
        w.executor.script(f"{id}-b1", FakeTurn(reports=(report("blocked", question="q?"),)))
        w.forge.fail_next_post = 2
        w.settle()
        self.assertEqual(sum("Factory is on it" in c for c in w.comments()), 0)
        self.assertEqual(w.engine.outbox.rows("comment")[0]["attempts"], 1)
        w.clock.advance(10)
        w.settle()
        w.clock.advance(10)
        w.settle()
        self.assertEqual(sum("Factory is on it" in c for c in w.comments()), 1)

    def test_executor_unavailable_retries_the_launch(self):
        w = self.world()
        id = w.issue()
        w.executor.down = True
        w.executor.script(f"{id}-b1", FakeTurn(reports=(report("blocked", question="q?"),)))
        w.settle()
        self.assertEqual(w.store.slots_used(), {"general": 1})
        w.executor.down = False
        w.clock.advance(10)
        w.settle()
        self.assertEqual(len(w.builder_texts(id)), 1)

    def test_a_permanent_executor_error_asks(self):
        w = self.world()
        id = w.issue()

        def refuse(_):
            raise ExecutorError("bad_request", "no such repo", retryable=False)

        w.executor.start = refuse
        w.settle()
        t = w.ticket(id)
        self.assertEqual(t.waiting_reason, WaitReason.TROUBLE)
        self.assertIn("no such repo", t.question)
        self.assertEqual(w.store.slots_used(), {})


class QuarantineTest(WorldTest):
    def test_only_an_uncheckable_write_quarantines(self):
        w = self.world()
        id = w.issue()
        w.executor.script(f"{id}-b1", FakeTurn(reports=(report("blocked", question="q?"),)))
        w.forge.cannot_check = True
        w.settle()
        t = w.ticket(id)
        self.assertTrue(t.quarantined)
        self.assertIn("lumbridge-factory retry", t.question)
        turns = len(w.builder_texts(id))
        w.forge.cannot_check = False
        w.engine.command("retry", id, "owner")
        w.executor.script(f"{id}-b1", FakeTurn(reports=(report("blocked", question="q2?"),)))
        w.settle()
        self.assertFalse(w.ticket(id).quarantined)
        self.assertGreater(len(w.builder_texts(id)), turns)
        self.assertEqual(sum("Factory is on it" in c for c in w.comments()), 1)


class SourceCursorTest(WorldTest):
    def _source(self, w):
        source = FakeSource()
        source.events.append(
            Trigger("gitea", "ev1", issue_thread(REPO, 1), TriggerKind.ISSUE_OPENED, "owner", repo=REPO, data={"labels": ["factory"], "title": "x"})
        )
        w.sources.append(source)
        w.engine.sources = tuple(w.sources)
        return source

    def test_restart_never_replays(self):
        w = self.world()
        source = self._source(w)
        w.settle()
        self.assertEqual(len(w.store.tickets()), 1)
        w.restart()
        w.engine.sources = tuple(w.sources)
        w.settle()
        self.assertEqual(source.polled_from[-1], "1", "the cursor survived the restart")
        self.assertEqual(len(w.store.tickets()), 1)

    def test_replay_of_a_seen_event_is_still_one_ticket(self):
        w = self.world()
        source = self._source(w)
        w.settle()
        with w.store.tx():
            w.store.set_cursor("gitea", "0", w.clock())
        w.settle()
        self.assertEqual(len(w.store.tickets()), 1)
        self.assertEqual(w.store.cursor("gitea"), "1")


class SourceDownTest(WorldTest):
    def test_a_source_down_is_not_a_crash_and_loses_nothing(self):
        w = self.world()
        source = FakeSource()
        source.events.append(
            Trigger("gitea", "ev1", issue_thread(REPO, 1), TriggerKind.ISSUE_OPENED, "owner", repo=REPO, data={"labels": ["factory"], "title": "x"})
        )
        working_poll, source.poll = source.poll, lambda cursor: (_ for _ in ()).throw(ConnectionError("down"))
        w.engine.sources = (source,)
        w.settle()
        self.assertEqual(w.store.tickets(), [])
        source.poll = working_poll
        w.settle()
        self.assertEqual(len(w.store.tickets()), 1)


class ThreadRunnerTest(WorldTest):
    def test_the_threaded_runner_works_end_to_end(self):
        import time

        w = self.world()
        w.engine.runner = ThreadRunner()
        id = w.issue()
        w.executor.script(f"{id}-b1", FakeTurn(reports=(report("blocked", question="q?"),)))
        deadline = time.time() + 5
        while w.ticket(id).phase != Phase.WAITING and time.time() < deadline:
            w.engine.tick()
            time.sleep(0.005)
        self.assertEqual(w.ticket(id).question, "q?")
