"""The state machine with no store at all: facts that disagree about which commit they're on."""

import unittest

from lumbridge_factory.core import tickets
from lumbridge_factory.core.facts import CiFacts, CiState, GateFacts, MainFacts, PrFacts, PrState, ReviewFacts, ReviewState
from lumbridge_factory.core.model import Mandate, Phase, Strength
from lumbridge_factory.core.policy import default_policy

HEAD, OLD, MAIN = "h" * 40, "o" * 40, "m" * 40
POLICY = default_policy()
BOT = POLICY.reviewer_bots[0]


def ticket(**changes):
    mandate = Mandate(
        "m-1", "t-1", 1, "a/b", "main", "a/b#issue/1", Strength.EXPLICIT, ("build",), "owner", "tr-1", 1000, 1000, 0.0
    )
    t = tickets.new_ticket(mandate, "x")
    from dataclasses import replace

    return replace(t, phase=Phase.GATING, pr=5, head_sha=HEAD, **changes)


def facts(ci_sha=HEAD, ci=CiState.GREEN, review_sha=HEAD, review=ReviewState.PASS, reviewer=BOT, contains=True, head=HEAD):
    return GateFacts(
        number=5,
        pr=PrFacts(5, PrState.OPEN, head, "factory/t-1", "main"),
        ci=CiFacts(ci_sha, ci),
        review=ReviewFacts(review_sha, review, reviewer),
        main=MainFacts(MAIN, contains),
    )


class ExactShaTest(unittest.TestCase):
    def step(self, t, f):
        return tickets.gate_step(t, f, POLICY, 0.0)

    def test_all_three_on_one_sha_accepts(self):
        tr = self.step(ticket(), facts())
        self.assertEqual((tr.ticket.phase, tr.ticket.accepted_sha), (Phase.READY, HEAD))

    def test_green_on_another_sha_isnt_green(self):
        tr = self.step(ticket(), facts(ci_sha=OLD))
        self.assertEqual(tr.ticket.phase, Phase.GATING)
        self.assertIsNone(tr.ticket.accepted_sha)
        self.assertIsNone(tr.start_reviewer_sha)

    def test_a_pass_on_another_sha_isnt_a_pass(self):
        tr = self.step(ticket(), facts(review_sha=OLD))
        self.assertIsNone(tr.ticket.accepted_sha)
        self.assertEqual(tr.start_reviewer_sha, HEAD)

    def test_a_pass_from_anyone_else_isnt_a_pass(self):
        tr = self.step(ticket(), facts(reviewer="owner"))
        self.assertIsNone(tr.ticket.accepted_sha)
        self.assertEqual(tr.start_reviewer_sha, HEAD)

    def test_not_containing_main_isnt_accepted(self):
        tr = self.step(ticket(), facts(contains=False))
        self.assertIsNone(tr.ticket.accepted_sha)
        self.assertEqual([e.kind for e in tr.effects], ["update_branch"])

    def test_the_update_is_asked_once_per_head_and_main(self):
        first = self.step(ticket(), facts(contains=False))
        second = self.step(first.ticket, facts(contains=False))
        self.assertEqual(second.effects, ())

    def test_a_new_head_withdraws_an_acceptance(self):
        accepted = self.step(ticket(), facts()).ticket
        tr = self.step(accepted, facts(head="n" * 40, ci_sha="n" * 40, ci=CiState.PENDING, review_sha="n" * 40, review=ReviewState.NONE))
        self.assertEqual(tr.ticket.phase, Phase.GATING)
        self.assertIsNone(tr.ticket.accepted_sha)
        self.assertEqual(tr.ticket.head_sha, "n" * 40)

    def test_red_after_acceptance_withdraws_it_and_wakes_the_builder(self):
        accepted = self.step(ticket(), facts()).ticket
        tr = self.step(accepted, facts(ci=CiState.RED))
        self.assertEqual(tr.ticket.phase, Phase.BUILDING)
        self.assertIsNone(tr.ticket.accepted_sha)
        self.assertEqual(len(tr.inputs), 1)

    def test_a_stable_ready_ticket_does_nothing(self):
        accepted = self.step(ticket(), facts()).ticket
        tr = self.step(accepted, facts())
        self.assertEqual(tr.ticket, accepted)
        self.assertEqual(tr.effects, ())


class StopIsForGoodTest(unittest.TestCase):
    def test_a_stopped_ticket_takes_no_follow_up_and_no_context(self):
        from lumbridge_factory.core.model import Trigger, TriggerKind

        stopped = tickets.stop(ticket(), "owner").ticket
        comment = Trigger("gitea", "c", "a/b#issue/1", TriggerKind.COMMENT, "owner", body="carry on", repo="a/b")
        for step in (tickets.follow_up, tickets.context_only):
            tr = step(stopped, comment)
            self.assertEqual(tr.ticket, stopped)
            self.assertEqual(tr.inputs, ())
        self.assertEqual(tickets.retry(stopped).ticket, stopped)


class WireTest(unittest.TestCase):
    def test_contract_round_trips(self):
        from lumbridge_factory.core import executor as x

        report = x.Report(x.ReportStatus.READY, "s", 3, "q?", x.Verdict.PASS, "f")
        entries = (
            x.InboxEntry(1, x.EntryKind.REPORT, report=report),
            x.InboxEntry(2, x.EntryKind.REPLY, purpose="p", text="t"),
            x.InboxEntry(3, x.EntryKind.HANDBACK, trigger="tr", text="why"),
        )
        result = x.TurnResult("t", "s", x.TurnOutcome.COMPLETED, "n", 1, 2.0, entries, "e")
        self.assertEqual(x.result_from_wire(x.to_wire(result)), result)
        event = x.TurnEvent("t", 1, x.EventType.USAGE, {"tokens": 3})
        self.assertEqual(x.event_from_wire(x.to_wire(event)), event)
        turn = x.TurnRequest("t", "s", "n", (x.TurnInput(x.InputKind.NOTE, "hi", "r"),), "a", "m", 9)
        self.assertEqual(x.turn_request_from_wire(x.to_wire(turn)), turn)
        start = x.StartRequest("t", "a", x.Role.REVIEWER, x.WorktreeSpec("r", "n", "", "main", "sha", True), x.PromptParts("s", "r"), "a", "m")
        self.assertEqual(x.start_request_from_wire(x.to_wire(start)), start)

    def test_unknown_keys_are_ignored(self):
        from lumbridge_factory.core import executor as x

        d = {"turn_id": "t", "session_id": "s", "outcome": "completed", "new_field": 1}
        self.assertEqual(x.result_from_wire(d).outcome, x.TurnOutcome.COMPLETED)

    def test_the_fake_is_an_executor(self):
        from lumbridge_factory.core.executor import Executor
        from lumbridge_factory.fakes import FakeExecutor

        self.assertIsInstance(FakeExecutor(), Executor)


class CarriedReviewTest(unittest.TestCase):
    """Opt-in per repo: a pass carries to a head that only took in main, when the PR's diff
    against main is the same change. Only CI repeats."""

    def setUp(self):
        from dataclasses import replace
        from lumbridge_factory.core.policy import RepoPolicy

        self.on = replace(POLICY, repositories=(RepoPolicy(name="a/b", required_checks=("CI / checks (pull_request)",), carry_review_across_main=True),))
        self.off = replace(POLICY, repositories=(RepoPolicy(name="a/b", required_checks=("CI / checks (pull_request)",)),))

    def f(self, change="fp-1", **kw):
        from dataclasses import replace

        base = facts(**kw)
        return replace(base, pr=replace(base.pr, change=change))

    def test_a_pass_records_the_change_it_passed(self):
        tr = tickets.gate_step(ticket(), self.f(), self.on, 0.0)
        self.assertEqual(tr.ticket.passed_change, f"{HEAD} fp-1")
        self.assertEqual(tr.ticket.accepted_sha, HEAD)

    def test_same_change_on_a_new_head_carries_the_pass(self):
        t = ticket(passed_change=f"{OLD} fp-1")
        tr = tickets.gate_step(t, self.f(review_sha=HEAD, review=ReviewState.NONE), self.on, 0.0)
        self.assertIsNone(tr.start_reviewer_sha, "no new review for the same change")
        self.assertEqual((tr.ticket.phase, tr.ticket.accepted_sha), (Phase.READY, HEAD))
        self.assertTrue(any("carries" in e.payload.get("text", "") for e in tr.effects), "the ready comment says so")

    def test_ci_still_has_to_pass_on_the_new_head(self):
        t = ticket(passed_change=f"{OLD} fp-1")
        tr = tickets.gate_step(t, self.f(ci_sha=OLD, review=ReviewState.NONE), self.on, 0.0)
        self.assertIsNone(tr.ticket.accepted_sha)
        tr = tickets.gate_step(t, self.f(ci=CiState.PENDING, review=ReviewState.NONE), self.on, 0.0)
        self.assertIsNone(tr.ticket.accepted_sha)

    def test_main_must_still_be_contained(self):
        t = ticket(passed_change=f"{OLD} fp-1")
        tr = tickets.gate_step(t, self.f(review=ReviewState.NONE, contains=False), self.on, 0.0)
        self.assertIsNone(tr.ticket.accepted_sha)
        self.assertEqual([e.kind for e in tr.effects], ["update_branch"])

    def test_a_changed_diff_gets_a_new_review(self):
        t = ticket(passed_change=f"{OLD} fp-1")
        tr = tickets.gate_step(t, self.f(change="fp-2", review=ReviewState.NONE), self.on, 0.0)
        self.assertIsNone(tr.ticket.accepted_sha)
        self.assertEqual(tr.start_reviewer_sha, HEAD)

    def test_an_unknown_change_gets_a_new_review(self):
        t = ticket(passed_change=f"{OLD} fp-1")
        tr = tickets.gate_step(t, self.f(change=None, review=ReviewState.NONE), self.on, 0.0)
        self.assertEqual(tr.start_reviewer_sha, HEAD)

    def test_off_unless_the_repo_turns_it_on(self):
        t = ticket(passed_change=f"{OLD} fp-1")
        tr = tickets.gate_step(t, self.f(review=ReviewState.NONE), self.off, 0.0)
        self.assertEqual(tr.start_reviewer_sha, HEAD)
        self.assertIsNone(tr.ticket.accepted_sha)

    def test_changes_requested_on_the_head_still_counts(self):
        t = ticket(passed_change=f"{OLD} fp-1")
        tr = tickets.gate_step(t, self.f(review=ReviewState.CHANGES_REQUESTED), self.on, 0.0)
        self.assertIsNone(tr.ticket.accepted_sha)
        self.assertEqual(tr.ticket.phase, Phase.BUILDING)
        self.assertIsNone(tr.ticket.passed_change, "a rejection forgets the old pass")

    def test_owner_asking_for_a_fresh_review_forgets_the_old_pass(self):
        t = ticket(passed_change=f"{OLD} fp-1", rereview=True)
        tr = tickets.gate_step(t, self.f(review=ReviewState.NONE), self.on, 0.0)
        self.assertIsNone(tr.ticket.passed_change)
        self.assertEqual(tr.start_reviewer_sha, HEAD)

    def test_the_fingerprint_ignores_line_numbers_and_index_lines_only(self):
        from lumbridge_factory.gates import change_fingerprint

        a = "diff --git a/x b/x\nindex 111..222 100644\n--- a/x\n+++ b/x\n@@ -10,3 +10,4 @@ fn x\n ctx\n+new\n ctx2\n"
        moved = a.replace("index 111..222", "index 333..444").replace("@@ -10,3 +10,4 @@ fn x", "@@ -40,3 +40,4 @@ fn y")
        self.assertEqual(change_fingerprint(a), change_fingerprint(moved), "main moved the lines: same change")
        self.assertNotEqual(change_fingerprint(a), change_fingerprint(a.replace("+new", "+newer")), "an edit isn't")
        self.assertNotEqual(change_fingerprint(a), change_fingerprint(a.replace(" ctx2", " other")), "nor is new context")
        self.assertNotEqual(change_fingerprint(a), change_fingerprint(a.replace("b/x", "b/y")), "nor another file")
