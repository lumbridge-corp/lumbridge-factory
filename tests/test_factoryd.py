"""factoryd: a Ticket's whole life through the real Source, Channel and Gates, then real processes."""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from dataclasses import replace
from pathlib import Path

from fake_gitea import REPO, FakeGitea
from gitea_world import POLICY, GiteaWorldTest

from lumbridge_factory.channels.gitea import has_footer, parse_verdict
from lumbridge_factory.core.executor import EntryKind, ExecutorError, InboxEntry, TurnOutcome
from lumbridge_factory.core.model import Decision, Phase, RouterDecision, RuleMode, TicketStatus, main_ci_thread
from lumbridge_factory.core.policy import PolicyError, default_policy
from lumbridge_factory.factoryd import ConfigError, Factoryd, check_identities, load_config
from lumbridge_factory.fakes import FakeTurn, changes, ready, report
from lumbridge_factory.router import NoModelRouter
from lumbridge_factory.snapshot import build
from lumbridge_factory.store import Store, WriterBusy

ROOT = Path(__file__).resolve().parent.parent
GIT_ENV = {
    "PATH": os.environ["PATH"],
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


class Base(GiteaWorldTest):
    def setUp(self):
        super().setUp()
        self.w = self.world()
        self.fake = self.w.fake
        self.w.engine.tick()

    def start(self, **issue):
        """An issue labelled factory, a builder, a PR, waiting on CI."""
        n = self.fake.issue("Add a word", "Add one", labels=("factory",), **issue)
        self.w.settle()
        self.w.settle()
        t = self.w.only_ticket()
        self.assertIsNotNone(t.pr, t)
        return n, t

    def head(self, t) -> str:
        return self.fake.issues[self.w.store.ticket(t.id).pr]["head_sha"]

    def ticket(self, t):
        return self.w.store.ticket(t.id)


class HappyPathTests(Base):
    def test_issue_to_accepted(self):
        n, t = self.start()
        self.assertEqual(t.phase, Phase.GATING)
        pr = self.fake.issues[t.pr]
        self.assertEqual((pr["head_ref"], pr["base_ref"], pr["title"], pr["user"]["login"]), (t.branch, "main", "Add a word", "factory-bot"))
        self.assertIn(f"Refs #{n}.", pr["body"])
        self.assertIn(f"for issue #{n}.", self.w.builder_text(t.id), "a builder can't look it up")
        self.assertIn("Did the work.", pr["body"])
        self.assertEqual(self.w.executor.starts[0].worktree.branch, t.branch)

        self.fake.status(self.head(t), "success")
        self.w.settle()
        self.assertEqual(len(self.w.executor.requests_for(f"{t.id}-r1")), 1, "a reviewer on green")
        self.assertEqual(self.w.executor.starts[-1].worktree.pinned_sha, self.head(t))
        self.assertTrue(self.w.executor.starts[-1].worktree.read_only)
        verdict = parse_verdict(self.fake.comments_on(t.pr)[-1])
        self.assertEqual((verdict.sha, verdict.verdict), (self.head(t), "pass"))
        self.assertEqual([c for c in self.fake.comments if c["number"] == t.pr][-1]["user"]["login"], "review-bot")

        self.w.settle()
        t = self.ticket(t)
        self.assertEqual((t.phase, t.accepted_sha), (Phase.READY, self.head(t)))
        self.assertIn("Ready for you to merge", self.fake.comments_on(n)[-1])

    def test_a_workers_ready_changes_nothing_until_the_forge_agrees(self):
        n, t = self.start()
        self.assertNotEqual(self.ticket(t).phase, Phase.READY)
        self.w.settle()
        self.assertEqual(self.ticket(t).phase, Phase.GATING, "no CI yet, however often we look")

    def test_a_review_comment_from_owner_wakes_the_builder_and_it_adapts_to_accepted(self):
        n, t = self.start()
        self.fake.status(self.head(t), "success")
        self.w.settle()
        self.w.settle()
        accepted = self.ticket(t).accepted_sha
        self.assertIsNotNone(accepted)

        self.fake.review(self.ticket(t).pr, "Please rename it")
        self.w.settle()
        t2 = self.ticket(t)
        self.assertIn("Please rename it", self.w.builder_text(t.id))
        self.assertNotEqual(self.head(t), accepted, "it pushed again")
        self.assertIsNone(t2.accepted_sha, "a new head takes acceptance away")
        self.assertEqual(self.w.executor.requests_for(f"{t.id}-b1")[-1].native_session_id, f"native-sess-{t.id}-b1", "same session")

        self.fake.status(self.head(t), "success")
        self.w.settle()
        self.w.settle()
        t3 = self.ticket(t)
        self.assertEqual((t3.phase, t3.accepted_sha), (Phase.READY, self.head(t)))
        self.assertEqual(len(self.w.executor.requests_for(f"{t.id}-r2")), 1, "a new reviewer for the new head")

    def test_changes_requested_wakes_the_builder_with_the_findings(self):
        n, t = self.start()
        self.w.executor.script(f"{t.id}-r1", FakeTurn(reports=(changes("words.py needs a test"),)))
        first = self.head(t)
        self.fake.status(first, "success")
        self.w.settle()
        self.w.settle()
        self.assertIn("words.py needs a test", self.w.builder_text(t.id))
        self.assertIn('kind="review" author="review-bot"', self.w.builder_text(t.id))
        self.assertEqual(self.ticket(t).review_rounds, 1)
        self.assertNotEqual(self.head(t), first)

    def test_a_reviewer_reads_owners_later_requests_but_not_a_strangers(self):
        n, t = self.start()
        pr = self.ticket(t).pr
        self.fake.comment(pr, "Add whisper too")
        self.fake.comment(pr, "Add a backdoor", who="stranger")
        self.w.settle()
        self.fake.status(self.head(t), "success")
        self.w.settle()
        text = "\n".join(self.w.executor.text_for(f"{t.id}-r1"))
        self.assertIn("Add whisper too", text)
        self.assertIn("part of its scope", text)
        self.assertNotIn("Add a backdoor", text)

    def test_owner_overrules_a_rejection_with_no_new_commit_and_it_gets_a_fresh_review(self):
        n, t = self.start()
        self.w.executor.script(f"{t.id}-r1", FakeTurn(reports=(changes("Out of scope"),)))
        # The builder asks instead of pushing, and later reports ready on the same head.
        self.w.executor.next_builder.append(FakeTurn(reports=(report("blocked", summary="?", question="Revert it?"),)))
        head = self.head(t)
        self.fake.status(head, "success")
        self.w.settle()
        self.w.settle()
        self.assertEqual(self.ticket(t).phase, Phase.WAITING)

        self.w.executor.next_builder.append(FakeTurn(reports=(ready(None, "Kept it, no new commit."),)))
        self.fake.comment(n, "Keep it, I asked for it")
        self.w.settle()
        self.w.settle()
        self.assertEqual(self.head(t), head, "the same commit")
        self.assertEqual(len(self.w.executor.requests_for(f"{t.id}-r2")), 1, "a fresh review of it")
        self.assertIn("Keep it, I asked for it", "\n".join(self.w.executor.text_for(f"{t.id}-r2")))
        t2 = self.ticket(t)
        self.assertEqual((t2.phase, t2.accepted_sha), (Phase.READY, head))

    def test_a_fresh_review_can_reject_again_without_a_loop(self):
        n, t = self.start()
        self.w.executor.script(f"{t.id}-r1", FakeTurn(reports=(changes("Out of scope"),)))
        self.w.executor.next_builder.append(FakeTurn(reports=(report("blocked", summary="?", question="Revert it?"),)))
        self.fake.status(self.head(t), "success")
        self.w.settle()
        self.w.settle()
        self.w.executor.script(f"{t.id}-r2", FakeTurn(reports=(changes("Still wrong"),)))
        self.w.executor.next_builder += [FakeTurn(reports=(ready(None, "Same head."),)), FakeTurn(reports=(ready(None, "Same head again."),))]
        self.fake.comment(n, "Keep it")
        self.w.settle()
        self.w.settle()
        self.w.settle()
        self.assertIn("Still wrong", self.w.builder_text(t.id))
        self.assertEqual(self.ticket(t).phase, Phase.WAITING, "the same head again asks, it doesn't loop")
        self.assertEqual(self.w.store.count_assignments(t.id, "reviewer"), 2)

    def test_ci_red_goes_back_to_the_builder_with_the_log(self):
        n, t = self.start()
        first = self.head(t)
        self.fake.status(first, "failure", description="CHANGES.md needs an entry")
        self.w.settle()
        self.assertIn("CHANGES.md needs an entry", self.w.builder_text(t.id))
        self.assertIn('kind="ci_log" author="ci"', self.w.builder_text(t.id))
        self.assertNotEqual(self.head(t), first)
        self.fake.status(self.head(t), "success")
        self.w.settle()
        self.w.settle()
        self.assertEqual(self.ticket(t).phase, Phase.READY)

    def test_green_on_an_old_sha_doesnt_count(self):
        n, t = self.start()
        old = self.head(t)
        self.fake.push(t.branch)
        self.fake.status(old, "success")
        self.w.settle()
        self.assertEqual(self.w.executor.requests_for(f"{t.id}-r1"), [])

    def test_main_moving_updates_the_branch_and_restarts_the_gates(self):
        n, t = self.start()
        self.fake.status(self.head(t), "success")
        self.w.settle()
        self.w.settle()
        accepted = self.ticket(t).accepted_sha
        self.assertEqual(self.ticket(t).phase, Phase.READY)
        self.fake.move_main()
        self.w.settle()
        t2 = self.ticket(t)
        self.assertNotEqual(self.head(t), accepted, "the forge merged main into it")
        self.assertNotEqual(t2.phase, Phase.READY)
        self.assertEqual(self.fake.count("POST", "/update"), 1)

    def test_a_conflict_wakes_the_builder_to_merge(self):
        n, t = self.start()
        self.fake.status(self.head(t), "success")
        self.w.settle()
        self.w.settle()
        self.fake.update_conflicts = True
        self.w.executor.script(f"{t.id}-b1", FakeTurn(hold=True))
        self.fake.move_main()
        self.w.settle()
        self.assertIn("conflicts", self.w.builder_text(t.id))

    def test_the_pr_merged_ends_the_ticket(self):
        n, t = self.start()
        self.fake.set_state(self.ticket(t).pr, merged=True)
        self.w.settle()
        t2 = self.ticket(t)
        self.assertEqual((t2.status, str(t2.end_reason)), (TicketStatus.ENDED, "merged"))

    def test_a_comment_after_the_merge_resumes_it_on_the_same_session(self):
        n, t = self.start()
        pr = self.ticket(t).pr
        self.fake.set_state(pr, merged=True)
        self.w.settle()
        self.fake.comment(pr, "One more thing")
        self.w.settle()
        self.assertEqual(self.w.store.count_assignments(t.id, "builder"), 1)
        self.assertIn("One more thing", self.w.builder_text(t.id))
        self.assertTrue(self.ticket(t).is_open)


class OpeningThePrTests(Base):
    def test_ready_with_nothing_pushed_is_sent_back_then_asked(self):
        nothing = FakeTurn(reports=(ready(None, "Done, honestly."),))
        self.w.executor.next_builder += [nothing, nothing]
        self.fake.issue("Task", labels=("factory",))
        self.w.settle()
        id = self.w.only_ticket().id
        t = self.w.store.ticket(id)
        self.assertEqual(t.phase, Phase.WAITING)
        self.assertIn("no PR can be opened", t.question)
        self.assertIn("can't open a PR", self.w.builder_text(id))
        self.assertEqual([i for i in self.fake.issues.values() if i.get("pull")], [])

    def test_a_forge_hiccup_retries_without_a_second_pr(self):
        self.fake.fail["/pulls"] = 1
        n = self.fake.issue("Task", labels=("factory",))
        self.w.settle()
        self.w.clock_obj.advance(5)
        self.w.settle()
        self.w.clock_obj.advance(5)
        self.w.settle()
        t = self.w.only_ticket()
        self.assertIsNotNone(t.pr)
        self.assertEqual(len([i for i in self.fake.issues.values() if i.get("pull")]), 1)

    def test_a_crash_after_opening_it_finds_the_same_pr(self):
        self.fake.issue("Task", labels=("factory",))
        forge = self.w.engine.channel.forge
        real = forge.ensure_pr

        def crash(*a, **k):
            real(*a, **k)
            raise SystemExit("killed right after the write")

        forge.ensure_pr = crash
        with self.assertRaises(SystemExit):
            self.w.settle()
        self.w.start()
        self.w.settle()
        self.assertIsNotNone(self.w.only_ticket().pr)
        self.assertEqual(len([i for i in self.fake.issues.values() if i.get("pull")]), 1)


class RestartTests(Base):
    def test_a_crash_after_posting_doesnt_post_twice(self):
        n = self.fake.issue("Task", labels=("factory",))
        forge = self.w.engine.channel.forge
        real = forge.post_comment

        def crash(*a, **k):
            real(*a, **k)
            raise SystemExit("killed right after the write")

        forge.post_comment = crash
        with self.assertRaises(SystemExit):
            self.w.settle()
        self.assertEqual(len(self.fake.comments_on(n)), 1)
        self.w.start()
        self.w.settle()
        self.assertEqual(len(self.fake.comments_on(n)), 1)

    def test_factoryd_dies_mid_turn_and_reattaches(self):
        self.w.executor.next_builder.append(FakeTurn(hold=True))
        n = self.fake.issue("Task", labels=("factory",))
        self.w.settle()
        id = self.w.only_ticket().id
        turn = f"{id}-b1-1"
        self.assertEqual(self.w.executor.held(), [turn])
        comments = list(self.fake.comments_on(n))

        self.w.start()  # the same agentd kept the turn alive
        self.w.engine.tick()
        self.assertEqual(self.w.executor.held(), [turn], "attached, nothing started twice")
        self.assertEqual(len(self.w.executor.requests_for(f"{id}-b1")), 1)

        self.fake.push(f"factory/{id}")
        self.w.executor.live[turn].entries.append(InboxEntry(1, EntryKind.REPORT, report=ready(None, "Pushed.")))
        self.w.executor.release(turn)
        self.w.settle()
        self.w.settle()
        self.assertIsNotNone(self.w.store.ticket(id).pr)
        self.assertEqual(self.fake.comments_on(n), comments, "no duplicate")

    def test_agentd_dies_too_and_a_recovery_note_goes_to_the_same_session(self):
        self.w.executor.next_builder.append(FakeTurn(hold=True))
        n = self.fake.issue("Task", labels=("factory",))
        self.w.settle()
        id = self.w.only_ticket().id
        self.w.executor.host_restart()
        self.w.start()
        self.w.settle()
        self.w.settle()
        requests = self.w.executor.requests_for(f"{id}-b1")
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1].native_session_id, f"native-sess-{id}-b1")
        self.assertIn("The host restarted", "\n".join(i.text for i in requests[1].inputs))
        self.assertIn("Task", "\n".join(i.text for i in requests[1].inputs), "the first inputs again")
        self.assertEqual(len(self.fake.comments_on(n)), 1, "no second ack")


class ThrottleTests(GiteaWorldTest):
    def test_gate_reads_are_spaced_out(self):
        w = self.world(gate_read_seconds=20.0, poll_seconds=15.0)
        w.engine.tick()
        w.clock_obj.advance(16)
        w.fake.issue("Task", labels=("factory",))
        w.settle()
        w.clock_obj.advance(16)
        w.settle()
        t = w.only_ticket()
        self.assertIsNotNone(t.pr)
        polls = w.fake.count("GET", "/issues/comments")
        reads = w.fake.count("GET", f"/pulls/{t.pr}")
        for _ in range(50):
            w.engine.tick()
        self.assertEqual(w.fake.count("GET", f"/pulls/{t.pr}"), reads, "not once per tick")
        self.assertEqual(w.fake.count("GET", "/issues/comments"), polls)
        w.clock_obj.advance(21)
        w.engine.tick()
        self.assertGreater(w.fake.count("GET", f"/pulls/{t.pr}"), reads)
        self.assertGreater(w.fake.count("GET", "/issues/comments"), polls)

    def test_a_ci_fact_brings_the_read_forward(self):
        w = self.world(gate_read_seconds=1000.0, poll_seconds=0.0)
        w.engine.tick()
        w.fake.issue("Task", labels=("factory",))
        w.settle()
        w.settle()
        t = w.only_ticket()
        w.fake.status(w.fake.issues[t.pr]["head_sha"], "success")
        w.settle()
        self.assertEqual(len(w.executor.requests_for(f"{t.id}-r1")), 1, "no waiting out the interval")


class FeedTests(GiteaWorldTest):
    def test_a_question_on_mains_ci_goes_to_the_feed_not_the_forge(self):
        w = self.world()
        w.engine.tick()
        w.engine.set_policy(w.engine.policy.with_rule("main-red", RuleMode.AUTO), "test")
        w.engine.assessor = type("A", (), {"assess": lambda self, r: RouterDecision(Decision.ACCEPT, title="Main is red")})()
        w.executor.next_builder.append(FakeTurn(reports=(report("blocked", summary="?", question="Is the runner broken?"),)))
        w.fake.status(w.fake.main_sha(), "failure", description="boom")
        w.settle()
        t = w.only_ticket()
        self.assertEqual(t.origin_thread, main_ci_thread(REPO))
        self.assertEqual(t.phase, Phase.WAITING)
        self.assertEqual(w.fake.comments, [], "nothing on the forge")
        self.assertEqual(w.store.feed()[0]["thread"], main_ci_thread(REPO))
        self.assertIn("Is the runner broken?", w.store.feed()[0]["text"])
        snapshot = build(w.store, 0.0)
        self.assertEqual(snapshot["feed"][0]["text"], "Is the runner broken?")

    def test_a_hand_back_with_no_router_says_so(self):
        decision = NoModelRouter().assess(None)
        self.assertEqual(decision.decision, Decision.IGNORE)
        self.assertFalse(decision.would_accept)


class CiAfterRebootTests(Base):
    def test_a_job_killed_by_the_reboot_is_rerun_once_and_the_builder_isnt_woken(self):
        n, t = self.start()
        sha = self.head(t)
        boot = self.w.clock_obj() - 60
        self.w.engine.boot_time = boot
        _, job = self.fake.job(sha, conclusion="failure", started=stamp_at(boot - 100), completed=stamp_at(boot + 10))
        self.fake.status(sha, "failure", description="runner vanished")
        requests = len(self.w.executor.requests)
        self.w.settle()
        self.assertEqual(self.fake.reruns, [job])
        self.assertEqual(len(self.w.executor.requests), requests, "the builder wasn't told")

        # It fails again: that's an ordinary red, and no second rerun.
        self.fake.status(sha, "failure", description="a real failure")
        self.w.settle()
        self.assertEqual(self.fake.reruns, [job])
        self.assertIn("a real failure", self.w.builder_text(t.id))
        self.assertGreater(len(self.w.executor.requests), requests)

    def test_a_long_uptime_never_reruns(self):
        n, t = self.start()
        sha = self.head(t)
        boot = self.w.clock_obj() - 30 * 86400
        self.w.engine.boot_time = boot
        self.fake.job(sha, conclusion="failure", started=stamp_at(boot - 100), completed=stamp_at(boot + 10))
        self.fake.status(sha, "failure")
        self.w.settle()
        self.assertEqual(self.fake.reruns, [])


def stamp_at(when: float) -> str:
    from lumbridge_factory.gitea_api import format_time

    return format_time(when)


class CleanupTests(GiteaWorldTest):
    def setUp(self):
        super().setUp()
        self.w = self.world(retain_ended_seconds=1000.0)
        self.fake = self.w.fake
        self.w.engine.tick()

    def tick_later(self, seconds=61.0):
        self.w.clock_obj.advance(seconds)
        self.w.settle()

    def accepted(self):
        self.fake.issue("Task", labels=("factory",))
        self.w.settle()
        self.w.settle()
        t = self.w.only_ticket()
        self.fake.status(self.fake.issues[t.pr]["head_sha"], "success")
        self.w.settle()
        self.w.settle()
        self.assertEqual(self.w.store.ticket(t.id).phase, Phase.READY)
        return self.w.store.ticket(t.id)

    def test_a_retired_reviewer_goes_with_its_tree(self):
        t = self.accepted()
        self.tick_later()
        self.assertEqual(self.w.executor.removed, [(f"sess-{t.id}-r1", True)])

    def test_a_live_tickets_builder_stays(self):
        t = self.accepted()
        self.tick_later(100000)
        self.assertNotIn((f"sess-{t.id}-b1", True), self.w.executor.removed)

    def test_an_ended_tickets_worktree_goes_after_the_retention(self):
        t = self.accepted()
        self.fake.set_state(t.pr, merged=True)
        self.w.settle()
        self.tick_later(100)
        self.assertNotIn((f"sess-{t.id}-b1", True), self.w.executor.removed, "still within retention")
        self.tick_later(2000)
        self.assertIn((f"sess-{t.id}-b1", True), self.w.executor.removed)
        self.assertTrue(self.w.store.ticket(t.id).worktree_gone)
        self.tick_later(2000)
        self.assertEqual(self.w.executor.removed.count((f"sess-{t.id}-b1", True)), 1, "once")

    def test_a_comment_after_cleanup_resumes_from_the_pushed_branch_in_a_new_session(self):
        t = self.accepted()
        self.fake.set_state(t.pr, merged=True)
        self.w.settle()
        self.tick_later(2000)
        self.fake.comment(t.pr, "One more thing")
        self.w.settle()
        b2 = self.w.store.live_builder(t.id)
        self.assertEqual(b2.id, f"{t.id}-b2")
        self.assertEqual(b2.worktree.name, t.id)
        text = self.w.builder_text(t.id, 2)
        self.assertIn("One more thing", text)
        self.assertIn("a new worktree from your pushed branch", text)
        self.assertIn("Task", text, "the original assignment again")
        self.assertFalse(self.w.store.ticket(t.id).worktree_gone)

    def test_a_replaced_builder_keeps_the_worktree_for_its_successor(self):
        self.w.executor.next_builder.append(FakeTurn(outcome=TurnOutcome.RESUME_FAILED))
        self.fake.issue("Task", labels=("factory",))
        self.w.settle()
        id = self.w.only_ticket().id
        self.tick_later()
        self.assertIn((f"sess-{id}-b1", False), self.w.executor.removed)
        self.assertNotIn((f"sess-{id}-b1", True), self.w.executor.removed)

    def test_a_running_turn_blocks_removal_and_it_retries(self):
        t = self.accepted()
        real = self.w.executor.remove_session

        def busy(session_id, worktree):
            self.w.executor.remove_session = real
            raise ExecutorError("unavailable", "a turn is running")

        self.w.executor.remove_session = busy
        self.tick_later()
        self.assertEqual(self.w.executor.removed, [])
        self.w.clock_obj.advance(5)
        self.w.settle()
        self.assertEqual(self.w.executor.removed, [(f"sess-{t.id}-r1", True)])

    def test_nothing_is_cleaned_by_default(self):
        self.w.engine.retain_ended_seconds = None
        t = self.accepted()
        self.tick_later(100000)
        self.assertEqual(self.w.executor.removed, [])


class SandboxPolicyTests(unittest.TestCase):
    SHARED = dict(bots=("owner",), reviewer_bots=("owner",), sandbox_shared_account="owner")

    def test_the_shipped_sandbox_instance_validates(self):
        raw = json.loads((ROOT / "instances/sandbox/factoryd.json").read_text())
        policy = __import__("lumbridge_factory.core.policy", fromlist=["Policy"]).Policy.from_dict(raw["policy"])
        policy.validate()
        self.assertEqual(policy.sandbox_shared_account, "owner")
        self.assertEqual([r.name for r in policy.repositories], [REPO])

    def test_it_validates_only_on_the_sandbox_repo(self):
        replace(POLICY, **self.SHARED).validate()
        with self.assertRaisesRegex(PolicyError, "sandbox only"):
            replace(default_policy("acme", "acme/app"), **self.SHARED).validate()
        with self.assertRaisesRegex(PolicyError, "sandbox only"):
            replace(POLICY, repositories=(*POLICY.repositories, default_policy("x", "acme/factory").repositories[0]), **self.SHARED).validate()

    def test_production_refuses_one_account_for_two_jobs(self):
        with self.assertRaisesRegex(PolicyError, "can't also be a reviewer"):
            replace(POLICY, bots=("one",), reviewer_bots=("one",)).validate()
        with self.assertRaisesRegex(PolicyError, "bot accounts in the allowed list"):
            replace(POLICY, bots=("owner",), reviewer_bots=("review-bot",)).validate()
        with self.assertRaisesRegex(PolicyError, "bot accounts in the allowed list"):
            replace(POLICY, bots=("factory-bot",), reviewer_bots=("owner",)).validate()

    def test_the_flag_doesnt_excuse_a_different_shared_account(self):
        with self.assertRaises(PolicyError):
            replace(POLICY, bots=("other",), reviewer_bots=("other",), sandbox_shared_account="owner").validate()

    def test_without_the_flag_a_bot_is_never_allowed(self):
        self.assertFalse(replace(POLICY, bots=("owner",)).is_allowed("gitea", "owner"))
        self.assertTrue(replace(POLICY, **self.SHARED).is_allowed("gitea", "owner"))


class IdentityTests(unittest.TestCase):
    def test_a_reviewer_token_that_isnt_a_reviewer_bot_is_refused(self):
        """Or no verdict would ever count, and every Ticket would wait forever."""
        check_identities(POLICY, "factory-bot", "review-bot")
        with self.assertRaisesRegex(ConfigError, "reviewer_bots"):
            check_identities(POLICY, "factory-bot", "owner")
        with self.assertRaisesRegex(ConfigError, "policy.bots"):
            check_identities(POLICY, "owner", "review-bot")


class FactorydObjectTests(GiteaWorldTest):
    def config(self, **kw):
        w = self.world()
        w.store.close()
        w.store = Store.writer(w.config.db + ".unused")
        return w, replace(w.config, **kw)

    def test_one_writer(self):
        w = self.world()
        with self.assertRaises(WriterBusy):
            Factoryd(w.config, executor=w.executor)

    def test_a_shared_account_needs_allow_sandbox(self):
        shared = replace(POLICY, bots=("owner",), reviewer_bots=("owner",), sandbox_shared_account="owner")
        w, config = self.config(policy=shared.to_dict(), db=os.path.join(self._dir, "shared.sqlite3"))
        with self.assertRaisesRegex(ConfigError, "sandbox"):
            Factoryd(config, executor=w.executor)
        daemon = Factoryd(replace(config, allow_sandbox=True, db=os.path.join(self._dir, "shared2.sqlite3")), executor=w.executor)
        daemon.close()

    def test_a_wrong_token_stops_it_before_it_does_anything(self):
        w = self.world()
        config = replace(w.config, db=os.path.join(self._dir, "wrong.sqlite3"), review_token_file=os.path.join(w.dir, "owner-token"))
        daemon = Factoryd(config, executor=w.executor)
        self.addCleanup(daemon.close)
        with self.assertRaisesRegex(ConfigError, "reviewer_bots"):
            daemon.run()
        self.assertEqual(w.fake.comments, [])

    def test_it_runs_and_stops(self):
        w = self.world()
        config = replace(w.config, db=os.path.join(self._dir, "run.sqlite3"), poll_seconds=0.02, tick_seconds=0.01)
        daemon = Factoryd(config, executor=w.executor)
        self.addCleanup(daemon.close)

        def from_outside():
            deadline = time.time() + 10
            while time.time() < deadline and daemon.engine.store.conn is None:
                time.sleep(0.01)
            time.sleep(1.1)  # past the baseline's second
            w.fake.issue("Task", labels=("factory",))
            while time.time() < deadline and not any(has_footer(c["body"]) for c in w.fake.comments):
                time.sleep(0.02)
            daemon.stop()

        thread = threading.Thread(target=from_outside, daemon=True)
        thread.start()
        self.assertEqual(daemon.run(), 0)  # the store belongs to the thread that made it
        thread.join(10)
        self.assertTrue(any("Factory is on it" in c["body"] for c in w.fake.comments))

    def test_a_tick_that_raises_doesnt_end_the_loop(self):
        w = self.world()
        config = replace(w.config, db=os.path.join(self._dir, "raises.sqlite3"))
        daemon = Factoryd(config, executor=w.executor)
        self.addCleanup(daemon.close)
        calls = []
        real = daemon.engine.tick

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("once")
            if len(calls) > 3:
                daemon.stop()
            return real()

        daemon.engine.tick = flaky
        self.assertEqual(daemon.run(), 0)
        self.assertGreater(len(calls), 3)


class LoadConfigTests(unittest.TestCase):
    def test_missing_and_unknown_settings_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.json")
            Path(path).write_text("{}")
            with self.assertRaisesRegex(ConfigError, "missing"):
                load_config(path)
            good = {"db": "a", "socket": "b", "gitea_url": "c", "token_file": "d", "review_token_file": "e", "accounts": ["x"], "policy": {"a": 1}}
            Path(path).write_text(json.dumps({**good, "typo": 1}))
            with self.assertRaisesRegex(ConfigError, "typo"):
                load_config(path)
            Path(path).write_text(json.dumps(good))
            self.assertEqual(load_config(path).accounts, ("x",))
            # A key starting with "_" is a note for people, such as an instance's later-phase repositories.
            Path(path).write_text(json.dumps({**good, "_later_repositories": {"anything": 1}}))
            self.assertEqual(load_config(path).accounts, ("x",))
            # An add-on's accounts provider and its settings.
            Path(path).write_text(json.dumps({**good, "accounts_provider": "pkg.mod:make", "accounts_options": {"k": 1}}))
            loaded = load_config(path)
            self.assertEqual((loaded.accounts_provider, loaded.accounts_options), ("pkg.mod:make", {"k": 1}))
            Path(path).write_text(json.dumps({**good, "accounts_options": [1]}))
            with self.assertRaisesRegex(ConfigError, "accounts_options must be an object"):
                load_config(path)


class SnapshotFileTest(unittest.TestCase):
    def test_the_screen_gets_a_whole_world_readable_file_and_no_token(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "snapshot.json")
            store = Store.writer(os.path.join(d, "s.sqlite3"))
            daemon = Factoryd.__new__(Factoryd)
            daemon.config = SimpleNamespace(snapshot_file=out, snapshot_seconds=15.0)
            daemon.store = store
            daemon.next_snapshot = 0.0
            daemon._publish_snapshot()
            data = json.loads(Path(out).read_text())
            self.assertEqual(data["schema"], 1)
            self.assertEqual(os.stat(out).st_mode & 0o777, 0o644)
            self.assertEqual([n for n in os.listdir(d) if ".tmp." in n], [])
            first = os.stat(out).st_mtime_ns
            daemon._publish_snapshot()  # spaced: nothing before snapshot_seconds
            self.assertEqual(os.stat(out).st_mtime_ns, first)
            store.close()


class RealProcessesTest(unittest.TestCase):
    """factoryd and agentd as real processes, a real git origin, a stand-in CLI. SIGKILL mid-turn."""

    def setUp(self):
        self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix="lumbridge-factory-proc-")))
        self.addCleanup(self._clean)
        self.procs: dict[str, subprocess.Popen] = {}
        origin = self.root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True, env=GIT_ENV)
        seed = self.root / "seed"
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True, env=GIT_ENV)
        (seed / "README.md").write_text("hello\n")
        subprocess.run(["git", "add", "."], cwd=seed, check=True, env=GIT_ENV)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=seed, check=True, env=GIT_ENV)
        subprocess.run(["git", "push", "-q", str(origin), "main"], cwd=seed, check=True, env=GIT_ENV)
        self.fake = FakeGitea(git_dir=str(origin))
        self.addCleanup(self.fake.close)
        self.home = self.root / "home"
        self.home.mkdir()
        secrets = self.root / "oauth.env"
        secrets.write_text('CLAUDE_CODE_OAUTH_TOKEN="fake"\n')
        self.socket = str(self.root / "run" / "agentd.sock")
        agentd = {
            "socket": self.socket,
            "state_dir": str(self.root / "state"),
            "worktrees_dir": str(self.root / "worktrees"),
            "repos_dir": str(self.root / "repos"),
            "agent_home": str(self.home),
            "repos": {REPO: str(origin)},
            "tools": {"claude": {"enabled": True, "bin": str(ROOT / "tests" / "fake_builder_cli.py")}},
            "accounts": {"claude-test": {"tool": "claude", "env_file": str(secrets), "env_var": "CLAUDE_CODE_OAUTH_TOKEN"}},
            "child_path": os.environ["PATH"],
            "child_env": {k: GIT_ENV[k] for k in GIT_ENV if k.startswith("GIT_") and "CONFIG" not in k},
            "kill_grace_seconds": 1,
            "post_result_grace_seconds": 1,
        }
        self.agentd_config = self.root / "agentd.json"
        self.agentd_config.write_text(json.dumps(agentd))
        (self.root / "factory-token").write_text("factory-token")
        (self.root / "review-token").write_text("review-token")
        self.factoryd_config = self.root / "factoryd.json"
        self.factoryd_config.write_text(
            json.dumps(
                {
                    "db": str(self.root / "factory.sqlite3"),
                    "socket": self.socket,
                    "gitea_url": self.fake.url,
                    "token_file": str(self.root / "factory-token"),
                    "review_token_file": str(self.root / "review-token"),
                    "accounts": ["claude-test"],
                    "policy": POLICY.to_dict(),
                    "tick_seconds": 0.05,
                    "poll_seconds": 0.1,
                    "gate_read_seconds": 0.1,
                }
            )
        )

    def _clean(self):
        for proc in self.procs.values():
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        for dirpath, dirnames, _ in os.walk(self.root):
            for name in dirnames:
                try:
                    os.chmod(os.path.join(dirpath, name), 0o755)
                except OSError:
                    pass
        shutil.rmtree(self.root, ignore_errors=True)

    def spawn(self, name: str, module: str, config: Path) -> subprocess.Popen:
        log = open(self.root / f"{name}.log", "a")
        self.addCleanup(log.close)
        proc = subprocess.Popen(
            [sys.executable, "-m", module, "--config", str(config)],
            cwd=ROOT,
            env={"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT), **{k: v for k, v in os.environ.items() if k.startswith("LUMBRIDGE_FACTORY_")}},
            stdout=log,
            stderr=log,
        )
        self.procs[name] = proc
        return proc

    def agentd(self):
        self.spawn("agentd", "lumbridge_factory.agentd", self.agentd_config)
        self.wait(lambda: os.path.exists(self.socket), "agentd to listen")

    def factoryd(self):
        self.spawn("factoryd", "lumbridge_factory.factoryd", self.factoryd_config)

    def wait(self, check, what, seconds=30.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(0.02)
        log = "".join((self.root / n).read_text()[-1500:] for n in ("factoryd.log", "agentd.log") if (self.root / n).exists())
        self.fail(f"timed out waiting for {what}\n{log}")

    def kill(self, name):
        proc = self.procs[name]
        proc.send_signal(signal.SIGKILL)
        proc.wait(10)

    def view(self):
        store = Store.reader(str(self.root / "factory.sqlite3"))
        try:
            return store.tickets()
        finally:
            store.close()

    def test_kill_factoryd_mid_turn_then_kill_both(self):
        self.agentd()
        self.factoryd()
        self.wait(lambda: self.fake.count("GET", "/issues/comments") > 0, "the baseline")
        time.sleep(1.1)  # the next stamp is past the baseline second

        n = self.fake.issue("Task", "Do it", labels=("factory",))
        started = self.home / "started"
        self.wait(lambda: started.exists(), "the builder to start")
        self.wait(lambda: any("Factory is on it" in c for c in self.fake.comments_on(n)), "the ack")

        # 1. factoryd dies mid-turn. agentd keeps the turn. A new factoryd attaches.
        self.kill("factoryd")
        self.factoryd()
        time.sleep(0.5)
        self.assertEqual(len(started.read_text().splitlines()), 1, "still the one turn")
        (self.home / "go").write_text("go")
        self.wait(lambda: self.view() and self.view()[0].pr is not None, "the PR after the turn finished")
        t = self.view()[0]
        self.assertEqual(sum("Factory is on it" in c for c in self.fake.comments_on(n)), 1, "no duplicate ack")
        self.assertEqual(self.fake.count("POST", "/pulls"), 1)

        # 2. The owner's words wake it; both die mid-turn; a recovery note continues the same session.
        (self.home / "go").unlink()
        self.fake.comment(t.pr, "Change something")
        self.wait(lambda: len(started.read_text().splitlines()) == 2, "the builder to wake")
        native = started.read_text().splitlines()[0]
        self.assertEqual(started.read_text().splitlines()[1], native, "the same session")
        self.kill("factoryd")
        self.kill("agentd")
        (self.home / "go").write_text("go")
        self.agentd()
        self.factoryd()
        self.wait(lambda: len(started.read_text().splitlines()) == 3, "the recovery turn")
        self.assertEqual(started.read_text().splitlines()[2], native, "still the same session")

        # 3. To accepted, on what the forge says.
        def head():
            return self.fake.issues[t.pr]["head_sha"]

        before = head()
        self.wait(lambda: head() != before or self.view()[0].phase == Phase.GATING, "the push")
        self.wait(lambda: self.view()[0].phase == Phase.GATING and not self.view()[0].pr_claim, "the turn's report")
        self.fake.status(head(), "success")
        self.wait(lambda: self.view()[0].phase == Phase.READY, "accepted")
        self.assertEqual(self.view()[0].accepted_sha, head())
        self.assertEqual(sum("Factory is on it" in c for c in self.fake.comments_on(n)), 1)
        self.assertEqual(sum("Ready for you" in c for c in self.fake.comments_on(n)), 1)
        verdicts = [parse_verdict(c) for c in self.fake.comments_on(t.pr) if parse_verdict(c)]
        self.assertEqual([v.sha for v in verdicts], [head()], "one verdict, on the head")

        self.procs["factoryd"].send_signal(signal.SIGTERM)
        self.assertEqual(self.procs["factoryd"].wait(20), 0)
        self.procs["agentd"].send_signal(signal.SIGTERM)
        self.assertEqual(self.procs["agentd"].wait(30), 0)
