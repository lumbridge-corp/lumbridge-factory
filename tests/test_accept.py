"""§5.1 and §4.1: what becomes a Ticket, and what a Ticket starts with."""

from lumbridge_factory.core.executor import InputKind, Role
from lumbridge_factory.core.model import Phase, Strength, Trigger, TriggerKind, issue_thread, ticket_id
from lumbridge_factory.core.policy import Policy, PolicyError, default_policy

from harness import REPO, WorldTest


class AcceptTest(WorldTest):
    def test_label_creates_ticket_mandate_assignment_and_one_ack(self):
        w = self.world()
        id = w.issue(title="Add a button")
        w.settle()
        t = w.ticket(id)
        self.assertEqual(t.branch, f"factory/{id}")
        m = w.store.mandate(t.mandate_id)
        self.assertEqual(m.strength, Strength.EXPLICIT)
        self.assertEqual(m.requested_by, "owner")
        self.assertEqual(m.rules, ("build",))
        self.assertEqual(m.policy_version, 1)
        acks = [c for c in w.comments() if "Factory is on it" in c]
        self.assertEqual(len(acks), 1)
        self.assertIn("Add a button", acks[0])
        start = w.executor.starts[0]
        self.assertEqual(start.role, Role.BUILDER)
        self.assertEqual(start.worktree.branch, f"factory/{id}")
        self.assertFalse(w.assessor.requests, "an explicit request needs no model")

    def test_slash_factory_comment_on_issue_accepts(self):
        w = self.world()
        w.comment(issue_thread(REPO, 5), "/factory")
        w.settle()
        self.assertEqual(len(w.store.tickets()), 1)

    def test_office_start_command_accepts(self):
        w = self.world()
        w.engine.ingest(
            Trigger(
                "office", "cmd-1", issue_thread(REPO, 5), TriggerKind.COMMAND, "owner", repo=REPO,
                data={"command": "start", "title": "From the button"},
            )
        )
        w.settle()
        self.assertEqual(w.store.tickets()[0].title, "From the button")

    def test_same_event_twice_is_one_ticket(self):
        w = self.world()
        trigger = Trigger(
            "gitea", "same", issue_thread(REPO, 1), TriggerKind.ISSUE_OPENED, "owner", repo=REPO, data={"labels": ["factory"]}
        )
        self.assertTrue(w.engine.ingest(trigger))
        self.assertFalse(w.engine.ingest(trigger))
        w.settle()
        self.assertEqual(len(w.store.tickets()), 1)
        self.assertEqual(len([c for c in w.comments() if "Factory is on it" in c]), 1)

    def test_unlabeled_issue_only_observes(self):
        w = self.world()
        w.issue(labels=())
        w.settle()
        self.assertEqual(w.store.tickets(), [])
        evals = w.store.evals("triage")
        self.assertEqual(evals[0]["mode"], "observe")
        self.assertEqual(w.comments(), [])

    def test_trigger_and_mandate_rows_are_immutable(self):
        w = self.world()
        id = w.issue()
        import sqlite3

        with self.assertRaises(sqlite3.IntegrityError):
            w.store.conn.execute("UPDATE triggers SET body = 'changed'")
        with self.assertRaises(sqlite3.IntegrityError):
            w.store.conn.execute("DELETE FROM triggers")
        with self.assertRaises(sqlite3.IntegrityError):
            w.store.conn.execute("UPDATE mandates SET requested_by = 'mallory'")
        self.assertEqual(w.ticket(id).requested_by, "owner")

    def test_first_input_is_the_assignment(self):
        w = self.world()
        id = w.issue(body="Please add a button")
        w.settle()
        first = w.executor.requests_for(f"{id}-b1")[0]
        self.assertEqual(first.inputs[0].kind, InputKind.ASSIGNMENT)
        self.assertIn("Please add a button", first.inputs[0].text)
        self.assertIsNone(first.native_session_id)

    def test_builder_with_no_report_gets_one_reminder_then_a_question(self):
        w = self.world()
        id = w.issue()
        w.settle()
        self.assertEqual(len(w.builder_texts(id)), 2, "one turn plus one reminder")
        t = w.ticket(id)
        self.assertEqual(t.phase, Phase.WAITING)
        self.assertIn("without a usable report", t.question)


class InstanceTest(WorldTest):
    def test_refuses_undecided_instance(self):
        from dataclasses import replace

        p = default_policy()
        for changes in (
            {"allowed": {"gitea": ()}},
            {"repositories": ()},
            {"data_rules": ""},
            {"outside_action_rules": " "},
            {"company_clauses": ""},
            {"merge_policy": ""},
            {"bots": ()},
            {"reviewer_bots": ()},
        ):
            with self.subTest(changes), self.assertRaises(PolicyError):
                replace(p, **changes).validate()

    def test_refuses_required_checks_undecided(self):
        from dataclasses import replace
        from lumbridge_factory.core.policy import RepoPolicy

        with self.assertRaises(PolicyError):
            replace(default_policy(), repositories=(RepoPolicy("a/b"),)).validate()

    def test_refuses_bot_on_the_allowed_list(self):
        p = default_policy()
        with self.assertRaises(PolicyError):
            p.with_allowed("gitea", ("owner", p.bots[0])).validate()

    def test_default_is_owner_only_everywhere(self):
        p = default_policy()
        self.assertTrue(all(people == ("owner",) for people in p.allowed.values()))

    def test_policy_round_trips(self):
        p = default_policy()
        self.assertEqual(Policy.from_dict(p.to_dict()), p)

    def test_ticket_id_is_stable(self):
        t = Trigger("gitea", "x", "a/b#issue/1", TriggerKind.COMMENT, "owner")
        self.assertEqual(ticket_id(t), ticket_id(t))
