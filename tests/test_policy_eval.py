"""§4.2, §4.4: observe mode, policy changes, one Eval per Thread."""

from lumbridge_factory.core.model import Decision, RouterDecision, RuleMode, Strength, TriggerKind, issue_thread, main_ci_thread
from lumbridge_factory.core.policy import PolicyError
from lumbridge_factory.fakes import FakeTurn, report

from harness import REPO, WorldTest


class ObserveTest(WorldTest):
    def test_main_red_observes_by_default(self):
        w = self.world()
        w.assessor.answer = RouterDecision(Decision.ACCEPT, note="looks real")
        w.fact(TriggerKind.CI_STATUS, main_ci_thread(REPO), state="red")
        w.settle()
        self.assertEqual(w.store.tickets(), [], "act on nothing")
        ev = w.store.evals("main-red")[0]
        self.assertEqual((ev["mode"], ev["decision"], ev["would_accept"]), ("observe", "ignore", 1))
        self.assertEqual(w.forge.comments, [])

    def test_green_main_doesnt_reach_the_router(self):
        w = self.world()
        w.fact(TriggerKind.CI_STATUS, main_ci_thread(REPO), state="green")
        w.settle()
        self.assertEqual(w.assessor.requests, [])

    def test_triggers_arriving_during_an_eval_batch_into_the_next(self):
        w = self.world()
        first = w.fact(TriggerKind.CI_STATUS, main_ci_thread(REPO), state="red")
        second = w.fact(TriggerKind.CI_STATUS, main_ci_thread(REPO), state="red")
        third = w.fact(TriggerKind.CI_STATUS, main_ci_thread(REPO), state="red")
        evals = w.store.evals("main-red")
        self.assertEqual(len(evals), 1, "one running")
        self.assertEqual(evals[0]["trigger_ids"], [first.id])
        self.assertEqual(w.store.routing(second.id)["status"], "pending")
        w.settle()
        evals = w.store.evals("main-red")
        self.assertEqual(len(evals), 2)
        self.assertEqual(evals[1]["trigger_ids"], [second.id, third.id])

    def test_router_down_keeps_the_trigger_for_later(self):
        w = self.world()
        w.assessor.down = True
        trigger = w.fact(TriggerKind.CI_STATUS, main_ci_thread(REPO), state="red")
        w.settle()
        self.assertEqual(w.store.routing(trigger.id)["status"], "pending")
        self.assertEqual(w.store.evals("main-red")[-1]["decision"], "unavailable")
        n = len(w.store.evals("main-red"))
        w.engine.tick()
        self.assertEqual(len(w.store.evals("main-red")), n, "not hammering it")
        w.assessor.down = False
        w.clock.advance(301)
        w.settle()
        self.assertEqual(w.store.routing(trigger.id)["status"], "observed")

    def test_router_note_is_cut_to_30_words(self):
        w = self.world()
        w.engine.set_policy(w.engine.policy.with_rule("main-red", RuleMode.AUTO), "owner")
        w.assessor.answer = RouterDecision(Decision.ACCEPT, note="word " * 80)
        w.fact(TriggerKind.CI_STATUS, main_ci_thread(REPO), state="red")
        w.settle()
        self.assertLessEqual(len(w.store.evals("main-red")[0]["note"].split()), 30)


class PolicyChangeTest(WorldTest):
    def test_main_red_to_auto_makes_an_inferred_quiet_ticket(self):
        w = self.world()
        w.assessor.answer = RouterDecision(Decision.ACCEPT)
        w.engine.set_policy(w.engine.policy.with_rule("main-red", RuleMode.AUTO), "owner")
        w.fact(TriggerKind.CI_STATUS, main_ci_thread(REPO), state="red")
        w.settle()
        t = w.store.tickets()[0]
        m = w.store.mandate(t.mandate_id)
        self.assertEqual((m.strength, m.rules, m.requested_by, m.policy_version), (Strength.INFERRED, ("main-red",), "rule:main-red", 2))
        self.assertFalse(any("Factory is on it" in c for c in w.forge.comments_on(main_ci_thread(REPO))), "no acknowledgement")
        self.assertIn("main-red", w.executor.starts[0].prompt_parts.rule_instructions)

    def test_a_second_red_doesnt_make_a_second_ticket(self):
        w = self.world()
        w.assessor.answer = RouterDecision(Decision.ACCEPT)
        w.engine.set_policy(w.engine.policy.with_rule("main-red", RuleMode.AUTO), "owner")
        w.fact(TriggerKind.CI_STATUS, main_ci_thread(REPO), state="red")
        w.settle()
        w.fact(TriggerKind.CI_STATUS, main_ci_thread(REPO), state="red")
        w.settle()
        self.assertEqual(len(w.store.tickets()), 1)

    def test_allowing_a_person_works_from_the_next_trigger(self):
        w = self.world()
        w.comment(issue_thread(REPO, 1), "/factory", actor="alex")
        w.settle()
        self.assertEqual(w.store.tickets(), [])
        w.engine.set_policy(w.engine.policy.with_allowed("gitea", ("owner", "alex")), "owner")
        w.comment(issue_thread(REPO, 2), "/factory", actor="alex")
        w.settle()
        self.assertEqual(len(w.store.tickets()), 1)
        self.assertEqual(w.store.tickets()[0].requested_by, "alex")

    def test_removing_a_person_stops_their_follow_ups(self):
        w = self.world()
        w.engine.set_policy(w.engine.policy.with_allowed("gitea", ("owner", "alex")), "owner")
        id = w.issue(actor="alex")
        w.executor.script(f"{id}-b1", FakeTurn(reports=(report("blocked", question="q?"),)))
        w.settle()
        w.engine.set_policy(w.engine.policy.with_allowed("gitea", ("owner",)), "owner")
        turns = len(w.builder_texts(id))
        w.comment(issue_thread(REPO, 1), "more please", actor="alex")
        w.settle()
        self.assertEqual(len(w.builder_texts(id)), turns)

    def test_open_tickets_keep_their_mandates(self):
        w = self.world()
        id = w.issue()
        w.settle()
        v1 = w.store.mandate(w.ticket(id).mandate_id)
        w.engine.set_policy(w.engine.policy.with_rule("triage", RuleMode.OFF), "owner")
        self.assertEqual(w.store.mandate(w.ticket(id).mandate_id), v1)
        self.assertEqual(v1.policy_version, 1)
        self.assertEqual(w.engine.policy.version, 2)

    def test_triage_off_records_no_eval(self):
        w = self.world()
        w.engine.set_policy(w.engine.policy.with_rule("triage", RuleMode.OFF), "owner")
        w.issue(labels=())
        w.settle()
        self.assertEqual(w.store.evals(), [])

    def test_always_on_rules_cant_be_turned_off(self):
        w = self.world()
        for rule in ("build", "outbound-guard"):
            with self.assertRaises(PolicyError):
                w.engine.policy.with_rule(rule, RuleMode.OFF)

    def test_a_policy_that_empties_the_allowed_list_is_refused(self):
        w = self.world()
        policy = w.engine.policy.with_allowed("gitea", ()).with_allowed("office", ())
        with self.assertRaises(PolicyError):
            w.engine.set_policy(policy, "owner")
        self.assertEqual(w.engine.policy.version, 1)

    def test_a_comment_cant_change_policy(self):
        w = self.world()
        w.comment(issue_thread(REPO, 1), "/factory policy set-mode main-red auto")
        w.settle()
        self.assertEqual(w.engine.policy.rules["main-red"], RuleMode.OBSERVE)
        self.assertEqual(w.engine.policy.version, 1)
