"""§7: slots by turn, budgets that ask, accounts that wait."""

from lumbridge_factory.core import capacity
from lumbridge_factory.core.executor import Role, TurnOutcome
from lumbridge_factory.core.facts import CiState
from lumbridge_factory.core.model import Phase, WaitReason, issue_thread
from lumbridge_factory.fakes import FakeTurn, report

from harness import REPO, WorldTest


class CompeteTest(WorldTest):
    def test_two_tickets_compete_for_one_slot(self):
        w = self.world(slots=1, reserved_slots=0)
        a = w.issue(1, title="A")
        b = w.issue(2, title="B")
        w.executor.script(f"{a}-b1", FakeTurn(hold=True))
        w.executor.script(f"{b}-b1", FakeTurn(reports=(report("blocked", question="q?"),)))
        w.settle()
        self.assertEqual(w.executor.held(), [f"{a}-b1-1"])
        self.assertEqual(w.builder_texts(b), [], "the second waits")
        self.assertEqual(w.store.slots_used(), {"general": 1})
        w.executor.release(f"{a}-b1-1")
        w.settle()
        self.assertEqual(len(w.builder_texts(b)), 1, "and starts when the turn ends")

    def test_a_ticket_waiting_on_ci_holds_no_slot(self):
        w = self.world(slots=1, reserved_slots=0)
        a, _ = w.to_gating(1)
        self.assertEqual(w.ticket(a).phase, Phase.GATING)
        self.assertEqual(w.store.slots_used(), {})
        b = w.issue(2, title="B")
        w.executor.script(f"{b}-b1", FakeTurn(reports=(report("blocked", question="q?"),)))
        w.settle()
        self.assertEqual(len(w.builder_texts(b)), 1)
        self.assertEqual(w.store.slots_used(), {}, "and a question holds none")

    def test_reviewer_goes_before_a_new_builder(self):
        w = self.world(slots=1, reserved_slots=0)
        a, pr = w.to_gating(1)
        w.forge.green(pr)
        w.engine.advance_gates()
        # A new request by the owner would be urgent, so make a system-caused one.
        c = capacity.Candidate("new", "t2", Role.BUILDER, False, False, 0)
        r = capacity.Candidate("rev", "t1", Role.REVIEWER, False, False, 5)
        self.assertEqual(capacity.pick([c, r], 0, 0, 1, 0)[0][0].assignment_id, "rev")

    def test_priority_order(self):
        owner = capacity.Candidate("owner", "a", Role.BUILDER, True, True, 9)
        review = capacity.Candidate("review", "b", Role.REVIEWER, False, False, 8)
        working = capacity.Candidate("working", "c", Role.BUILDER, False, True, 7)
        fresh = capacity.Candidate("fresh", "d", Role.BUILDER, False, False, 6)
        order = [c.assignment_id for c, _ in capacity.pick([fresh, working, review, owner], 0, 0, 4, 0)]
        self.assertEqual(order, ["owner", "review", "working", "fresh"])

    def test_owner_has_a_reserved_slot_when_all_are_full(self):
        w = self.world(slots=1, reserved_slots=1)
        a = w.issue(1)
        w.executor.script(f"{a}-b1", FakeTurn(hold=True))
        w.settle()
        b = w.issue(2)  # The owner's request: urgent
        w.executor.script(f"{b}-b1", FakeTurn(hold=True))
        w.settle()
        self.assertEqual(sorted(w.executor.held()), sorted([f"{a}-b1-1", f"{b}-b1-1"]))
        self.assertEqual(w.store.slots_used(), {"general": 1, "reserved": 1})

    def test_a_system_caused_turn_cant_take_the_reserved_slot(self):
        chosen = capacity.pick([capacity.Candidate("sys", "t", Role.BUILDER, False, True, 0)], 1, 0, 1, 1)
        self.assertEqual(chosen, [])


class BudgetTest(WorldTest):
    def test_budget_asks_owner_and_keeps_everything(self):
        w = self.world(budget_tokens=150)
        id, pr = w.to_gating(1)  # the first turn used 100
        w.forge.red(pr, "broke")  # under budget: this wakes the builder, whose fix also goes red
        w.executor.script(f"{id}-b1", w.fixes(pr, ci=CiState.RED, tokens=100))
        w.settle()
        turns = len(w.builder_texts(id))
        self.assertEqual(turns, 2)
        self.assertEqual(w.ticket(id).tokens, 200)
        t = w.ticket(id)
        self.assertEqual((t.phase, t.waiting_reason), (Phase.WAITING, WaitReason.BUDGET))
        self.assertTrue(t.is_open, "not ended")
        self.assertFalse(t.quarantined, "not quarantined")
        self.assertEqual(len(w.builder_texts(id)), turns, "no new turn without the owner")
        self.assertEqual(w.executor.stopped, [], "nothing killed")
        self.assertEqual(w.store.slots_used(), {}, "no slot held while asking")
        self.assertEqual(sum("has used 200 of 150 tokens" in c for c in w.comments()), 1)
        for _ in range(5):
            w.engine.tick()
        self.assertEqual(sum("tokens" in c for c in w.comments()), 1, "asks once")

        # The owner answers: same session, the waiting CI failure and his words, a fresh budget.
        session = w.executor.requests_for(f"{id}-b1")[-1].session_id
        w.executor.script(f"{id}-b1", FakeTurn(reports=(report("blocked", question="q?"),)))
        w.comment(issue_thread(REPO, 1), "keep going")
        w.settle()
        last = w.executor.requests_for(f"{id}-b1")[-1]
        self.assertEqual(last.session_id, session)
        text = w.builder_texts(id)[-1]
        self.assertIn("keep going", text)
        self.assertIn("CI is red", text)
        t = w.ticket(id)
        self.assertEqual(t.budget_tokens, 200 + 150)

    def test_budget_exhausted_with_a_good_pr_still_reaches_ready(self):
        """Only more builder work asks. A finished PR doesn't stall on its review."""
        w = self.world(budget_tokens=50)
        id, pr = w.to_gating(1)
        self.assertTrue(w.ticket(id).over_budget)
        w.forge.green(pr)
        w.settle()
        self.assertEqual(w.ticket(id).phase, Phase.READY)

    def test_wall_clock_budget_asks_too(self):
        w = self.world(budget_wall_seconds=5)
        id, pr = w.to_gating(1)
        w.forge.red(pr)
        w.settle()
        self.assertEqual(w.ticket(id).waiting_reason, WaitReason.BUDGET)

    def test_a_system_trigger_doesnt_answer_the_budget_question(self):
        w = self.world(budget_tokens=50)
        id, pr = w.to_gating(1)
        w.forge.red(pr)
        w.settle()
        turns = len(w.builder_texts(id))
        w.forge.push(pr, ci=CiState.GREEN)
        w.settle()
        self.assertEqual(w.ticket(id).waiting_reason, WaitReason.BUDGET)
        self.assertEqual(len(w.builder_texts(id)), turns)

    def test_stop_answers_it_too(self):
        w = self.world(budget_tokens=50)
        id, pr = w.to_gating(1)
        w.forge.red(pr)
        w.settle()
        w.comment(issue_thread(REPO, 1), "stop")
        w.settle()
        self.assertTrue(w.ticket(id).stopped)


class AccountTest(WorldTest):
    def test_account_limit_switches_account_with_the_same_inputs(self):
        w = self.world()
        id = w.issue(body="the task")
        w.executor.script(f"{id}-b1", FakeTurn(outcome=TurnOutcome.ACCOUNT_LIMIT), FakeTurn(reports=(report("blocked", question="q?"),)))
        w.settle()
        requests = w.executor.requests_for(f"{id}-b1")
        self.assertEqual([r.account for r in requests], ["claude-a", "claude-b"])
        self.assertIn("the task", w.builder_texts(id)[1], "inputs delivered again")
        self.assertEqual(w.ticket(id).waiting_reason, WaitReason.QUESTION)

    def test_all_accounts_limited_waits_without_failing(self):
        w = self.world()
        w.accounts.limited = {"claude-a", "claude-b"}
        id = w.issue()
        w.settle()
        self.assertEqual(w.executor.requests, [])
        t = w.ticket(id)
        self.assertEqual(t.phase, Phase.BUILDING)
        self.assertFalse(t.quarantined)
        self.assertEqual(w.store.slots_used(), {})
        w.accounts.limited = set()
        w.executor.script(f"{id}-b1", FakeTurn(reports=(report("blocked", question="q?"),)))
        w.settle()
        self.assertEqual(len(w.builder_texts(id)), 1)

    def test_one_account_is_shared_when_its_all_there_is(self):
        w = self.world()
        w.accounts.names = ["only"]
        id, pr = w.to_gating(ci=CiState.GREEN)
        self.assertEqual(w.ticket(id).phase, Phase.READY)
