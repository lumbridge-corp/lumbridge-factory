"""§5: follow-up, handback, resume, stop, and the three gates."""

from lumbridge_factory.core.executor import Role, TurnOutcome
from lumbridge_factory.core.facts import CiState
from lumbridge_factory.core.model import Decision, EndReason, Phase, TicketStatus, WaitReason, issue_thread, pr_thread
from lumbridge_factory.fakes import FakeTurn, changes, passed, ready, report

from harness import REPO, WorldTest


class HappyPathTest(WorldTest):
    def test_issue_to_ready_for_owner(self):
        w = self.world()
        id, pr = w.to_gating()
        t = w.ticket(id)
        self.assertEqual((t.phase, t.pr, t.head_sha), (Phase.GATING, pr.number, pr.head_sha))
        subs = dict(w.store.subs_of_ticket(id))
        self.assertIn(pr_thread(REPO, pr.number), subs)
        self.assertEqual(w.store.count_assignments(id, Role.REVIEWER), 0, "no review before CI is green")

        w.forge.green(pr)
        w.settle()
        t = w.ticket(id)
        self.assertEqual(t.phase, Phase.READY)
        self.assertEqual(t.accepted_sha, pr.head_sha)
        self.assertEqual(t.status, TicketStatus.OPEN, "merging is the owner's")
        self.assertIn("Ready for you to merge", w.comments()[-1])

    def test_a_reviewer_reads_what_the_builder_says_it_ran(self):
        """widgets-app PR #3: the builder said it hadn't run the test the issue named, and the reviewer never saw that."""
        w = self.world()
        id, _ = w.to_gating(ci=CiState.GREEN)
        text = w.executor.text_for(f"{id}-r1")[0]
        self.assertIn("A claim, not a fact", text)
        self.assertIn('kind="report" author="builder"', text)

    def test_the_rules_about_running_named_checks_are_in_the_roles(self):
        from lumbridge_factory.core.prompts import BUILDER_SYSTEM, REVIEWER_SYSTEM
        self.assertIn("run every test or suite the request names", BUILDER_SYSTEM)
        self.assertIn("what you\nran and what it showed", BUILDER_SYSTEM)
        self.assertIn("changes_requested, with findings naming it, if a check the request names isn't shown passing", REVIEWER_SYSTEM)
        self.assertIn("relevant test wasn't run", REVIEWER_SYSTEM)

    def test_reviewer_is_independent_and_pinned(self):
        w = self.world()
        id, pr = w.to_gating(ci=CiState.GREEN)
        reviewer = next(s for s in w.executor.starts if s.role == Role.REVIEWER)
        builder = next(s for s in w.executor.starts if s.role == Role.BUILDER)
        self.assertEqual(reviewer.worktree.pinned_sha, pr.head_sha)
        self.assertTrue(reviewer.worktree.read_only)
        self.assertNotEqual(reviewer.assignment, builder.assignment)
        self.assertNotEqual(reviewer.worktree.name, builder.worktree.name)
        self.assertNotEqual(reviewer.account, builder.account, "a different account when two exist")
        self.assertNotEqual(reviewer.model, builder.model)

    def test_ready_is_posted_once_even_when_polled_again(self):
        w = self.world()
        w.to_gating(ci=CiState.GREEN)
        count = len(w.comments())
        for _ in range(5):
            w.engine.tick()
        self.assertEqual(len(w.comments()), count)


class FollowUpTest(WorldTest):
    def test_comment_during_a_turn_batches_into_the_next_turn(self):
        w = self.world()
        id = w.issue()
        w.executor.script(f"{id}-b1", FakeTurn(hold=True), FakeTurn(reports=(report("blocked", question="which color?"),)))
        w.settle()
        w.comment(issue_thread(REPO, 1), "make it blue")
        w.comment(issue_thread(REPO, 1), "and round")
        w.settle()
        self.assertEqual(len(w.builder_texts(id)), 1, "nothing is typed into a running turn")
        w.executor.release(f"{id}-b1-1")
        w.settle()
        second = w.builder_texts(id)[1]
        self.assertIn("make it blue", second)
        self.assertIn("and round", second)
        self.assertEqual(len(w.builder_texts(id)), 2, "one turn for both messages")

    def test_comment_on_issue_wakes_the_same_session(self):
        w = self.world()
        id, pr = w.to_gating()
        w.executor.script(f"{id}-b1", FakeTurn(reports=(ready(pr.number),)))
        w.comment(issue_thread(REPO, 1), "also add a label")
        w.settle()
        requests = w.executor.requests_for(f"{id}-b1")
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1].session_id, requests[0].session_id)
        self.assertEqual(requests[1].native_session_id, f"native-sess-{id}-b1", "resumes the CLI's session")
        self.assertEqual(w.ticket(id).phase, Phase.GATING)

    def test_comment_on_ready_ticket_reopens_the_work(self):
        w = self.world()
        id, pr = w.to_gating(ci=CiState.GREEN)
        self.assertEqual(w.ticket(id).phase, Phase.READY)
        w.executor.script(f"{id}-b1", w.fixes(pr))
        w.comment(pr_thread(REPO, pr.number), "one more thing")
        w.settle()
        t = w.ticket(id)
        self.assertEqual(t.phase, Phase.GATING)
        self.assertIsNone(t.accepted_sha)

    def test_question_is_posted_and_the_answer_goes_back(self):
        w = self.world()
        id = w.issue()
        w.executor.script(f"{id}-b1", FakeTurn(reports=(report("blocked", question="Which repo?"),)), FakeTurn(reports=(report("blocked", question="And the color?"),)))
        w.settle()
        t = w.ticket(id)
        self.assertEqual((t.phase, t.waiting_reason), (Phase.WAITING, WaitReason.QUESTION))
        self.assertIn("Which repo?", w.comments()[-1])
        self.assertEqual(len(w.builder_texts(id)), 1, "waiting holds no turn")
        w.comment(issue_thread(REPO, 1), "this one")
        w.settle()
        self.assertIn("this one", w.builder_texts(id)[1])

    def test_words_from_owner_during_a_turn_that_ends_asking_go_to_the_builder(self):
        """Found on the first real issue: the turn asked, and his comment sat in the queue with nothing to wake it."""
        w = self.world()
        id = w.issue()
        w.executor.script(f"{id}-b1", FakeTurn(hold=True, reports=(report("blocked", question="Allow it?"),)), FakeTurn(reports=(report("blocked", question="Next one?"),)))
        w.settle()
        w.comment(issue_thread(REPO, 1), "add a comment too")
        w.settle()
        w.executor.release(f"{id}-b1-1")
        w.settle()
        self.assertIn("Allow it?", w.comments())
        self.assertIn("add a comment too", w.builder_texts(id)[1])
        self.assertIn("Next one?", w.comments()[-1], "a question with nothing queued behind it still waits")
        self.assertEqual(w.ticket(id).phase, Phase.WAITING)
        self.assertEqual(len(w.builder_texts(id)), 2)

    def test_reply_is_posted_at_once_on_the_tickets_own_thread(self):
        w = self.world()
        id = w.issue()
        w.executor.script(f"{id}-b1", FakeTurn(replies=("On it.",), hold=True))
        w.settle()
        self.assertIn("On it.", w.comments())

    def test_chat_thread_replies_keep_to_the_word_limit(self):
        from lumbridge_factory.core import tickets
        from lumbridge_factory.core.policy import default_policy

        w = self.world()
        id = w.issue()
        w.settle()
        from dataclasses import replace

        t = replace(w.ticket(id), origin_thread="office:cmd/1")
        tr = tickets.worker_reply(t, "answer", "word " * 150, default_policy())
        self.assertEqual(len(tr.effects[0].payload["text"].split()), 100)

    def test_a_worker_can_follow_but_only_for_context(self):
        w = self.world()
        other = issue_thread(REPO, 9)
        id = w.issue()
        w.executor.script(f"{id}-b1", FakeTurn(follows=(other, "other/repo#issue/1"), reports=(report("blocked", question="q?"),)))
        w.settle()
        subs = dict(w.store.subs_of_ticket(id))
        self.assertEqual(subs.get(other), "context")
        self.assertNotIn("other/repo#issue/1", subs, "a worker can't reach outside its repo")
        turns = len(w.builder_texts(id))
        w.comment(other, "by the way")
        w.settle()
        self.assertEqual(len(w.builder_texts(id)), turns, "context wakes nothing")


class HandbackTest(WorldTest):
    def _with_follow_up(self, w, handback=True):
        id = w.issue()
        w.executor.script(f"{id}-b1", FakeTurn(hold=True))
        w.settle()
        trigger = w.comment(issue_thread(REPO, 1), "also rewrite the billing system")
        script = FakeTurn(handbacks=((trigger.id, "outside this ticket"),), reports=(report("blocked", question="Done here; anything else?"),))
        w.executor.script(f"{id}-b1", script)
        w.executor.release(f"{id}-b1-1")
        w.settle()
        return id, trigger

    def test_router_accept_makes_a_new_ticket(self):
        w = self.world()
        w.assessor.answer = w.assessor.answer.__class__(Decision.ACCEPT, note="separate work")
        id, trigger = self._with_follow_up(w)
        self.assertEqual(len(w.store.tickets()), 2)
        new = next(t for t in w.store.tickets() if t.id != id)
        mandate = w.store.mandate(new.mandate_id)
        self.assertEqual(mandate.requested_by, "owner")
        self.assertEqual(mandate.strength, "inferred")
        self.assertIn("also rewrite the billing system", w.assessor.requests[0].cards[0], "forwarded verbatim")
        self.assertIn("also rewrite the billing system", w.builder_texts(new.id)[0])

    def test_router_ignore_tells_owner(self):
        w = self.world()
        w.assessor.answer = w.assessor.answer.__class__(Decision.IGNORE)
        self._with_follow_up(w)
        self.assertEqual(len(w.store.tickets()), 1)
        self.assertTrue(any("handed this back" in c for c in w.comments()))

    def test_router_down_retries_later(self):
        w = self.world()
        w.assessor.down = True
        w.assessor.answer = w.assessor.answer.__class__(Decision.ACCEPT)
        self._with_follow_up(w)
        self.assertEqual(len(w.store.tickets()), 1)
        self.assertEqual(w.store.evals("handback")[-1]["decision"], "unavailable")
        w.assessor.down = False
        w.clock.advance(301)
        w.settle()
        self.assertEqual(len(w.store.tickets()), 2)

    def test_router_cant_attach_it_back_to_the_ticket_that_returned_it(self):
        w = self.world()
        id = w.issue()
        w.assessor.answer = w.assessor.answer.__class__(Decision.ATTACH, ticket_id=id)
        w.executor.script(f"{id}-b1", FakeTurn(hold=True))
        w.settle()
        trigger = w.comment(issue_thread(REPO, 1), "x")
        w.executor.script(f"{id}-b1", FakeTurn(handbacks=((trigger.id, "no"),), reports=(report("blocked", question="q?"),)))
        w.executor.release(f"{id}-b1-1")
        w.settle()
        self.assertEqual(w.ticket(id).phase, Phase.WAITING)
        self.assertEqual(len(w.builder_texts(id)), 2)

    def test_the_new_ticket_doesnt_take_the_thread_from_its_owner(self):
        w = self.world()
        w.assessor.answer = w.assessor.answer.__class__(Decision.ACCEPT)
        id, _ = self._with_follow_up(w)
        new = next(t for t in w.store.tickets() if t.id != id)
        self.assertEqual(dict(w.store.subs_of_ticket(new.id))[issue_thread(REPO, 1)], "context")
        w.executor.script(f"{id}-b1", FakeTurn(reports=(report("blocked", question="q?"),)))
        new_turns = len(w.builder_texts(new.id))
        w.comment(issue_thread(REPO, 1), "about the first thing")
        w.settle()
        self.assertIn("about the first thing", w.builder_texts(id)[-1])
        self.assertEqual(len(w.builder_texts(new.id)), new_turns, "only the owner wakes")

    def test_a_ticket_cant_hand_back_its_own_request(self):
        w = self.world()
        id = w.issue()
        request = w.store.mandate(f"m-{id[2:]}").trigger_id
        w.executor.script(f"{id}-b1", FakeTurn(handbacks=((request, "not mine"),), reports=(report("blocked", question="q?"),)))
        w.settle()
        self.assertEqual(w.store.evals("handback"), [])
        self.assertEqual(len(w.store.tickets()), 1)

    def test_a_worker_cant_hand_back_what_it_never_received(self):
        w = self.world()
        other = w.comment(issue_thread(REPO, 7), "nothing to do with you")
        id = w.issue()
        w.executor.script(f"{id}-b1", FakeTurn(handbacks=((other.id, "x"), ("tr-nonsense", "x")), reports=(report("blocked", question="q?"),)))
        w.settle()
        self.assertEqual(w.store.evals("handback"), [])


class ResumeTest(WorldTest):
    def test_merged_ticket_resumes_on_owner_comment_same_session(self):
        w = self.world()
        id, pr = w.to_gating(ci=CiState.GREEN)
        w.forge.merge(pr)
        w.settle()
        t = w.ticket(id)
        self.assertEqual((t.status, t.end_reason), (TicketStatus.ENDED, EndReason.MERGED))
        turns = len(w.builder_texts(id))

        w.executor.script(f"{id}-b1", FakeTurn(reports=(report("blocked", question="New PR?"),)))
        w.comment(pr_thread(REPO, pr.number), "this broke the docs")
        w.settle()
        t = w.ticket(id)
        self.assertEqual(t.status, TicketStatus.OPEN)
        self.assertEqual(t.mandate_id, f"m-{id[2:]}", "same Mandate")
        self.assertEqual(len(w.executor.starts), 2, "builder + reviewer: no new session started")
        requests = w.executor.requests_for(f"{id}-b1")
        self.assertEqual(len(requests), turns + 1)
        self.assertEqual(requests[-1].session_id, requests[0].session_id)
        self.assertIn("resumed", w.builder_texts(id)[-1])
        self.assertIn("this broke the docs", w.builder_texts(id)[-1])

    def test_closed_pr_later_comment_resumes(self):
        w = self.world()
        id, pr = w.to_gating()
        w.forge.close(pr)
        w.settle()
        self.assertEqual(w.ticket(id).end_reason, EndReason.CLOSED)
        new = w.opens_pr(id)
        w.executor.script(f"{id}-b1", new["turn"])
        w.comment(issue_thread(REPO, 1), "reopen this with a smaller change")
        w.settle()
        t = w.ticket(id)
        self.assertEqual(t.status, TicketStatus.OPEN)
        self.assertEqual(t.pr, new["pr"].number)

    def test_a_system_event_doesnt_resume(self):
        from lumbridge_factory.core.model import TriggerKind

        w = self.world()
        id, pr = w.to_gating(ci=CiState.GREEN)
        w.forge.merge(pr)
        w.settle()
        w.fact(TriggerKind.CI_STATUS, pr_thread(REPO, pr.number) + "/ci", state="red", sha=pr.head_sha)
        w.settle()
        self.assertEqual(w.ticket(id).status, TicketStatus.ENDED)


class StopHoldTest(WorldTest):
    def test_stop_comment_kills_the_turn_and_withdraws_for_good(self):
        w = self.world()
        id = w.issue()
        w.executor.script(f"{id}-b1", FakeTurn(hold=True))
        w.settle()
        w.comment(issue_thread(REPO, 1), "Stop.")
        w.settle()
        t = w.ticket(id)
        self.assertTrue(t.stopped)
        self.assertEqual(t.end_reason, EndReason.STOPPED)
        self.assertEqual(w.executor.stopped, [f"sess-{id}-b1"])
        self.assertEqual(w.executor.held(), [])
        self.assertEqual(w.store.slots_used(), {}, "the slot came back")

        turns = len(w.builder_texts(id))
        w.comment(issue_thread(REPO, 1), "actually, carry on")
        w.settle()
        self.assertTrue(w.ticket(id).stopped)
        self.assertEqual(len(w.builder_texts(id)), turns, "a plain comment doesn't undo a stop")

    def test_after_stop_an_explicit_request_is_a_new_ticket(self):
        w = self.world()
        id = w.issue()
        w.settle()
        w.comment(issue_thread(REPO, 1), "stop")
        w.comment(issue_thread(REPO, 1), "/factory try again")
        w.settle()
        self.assertEqual(len(w.store.tickets()), 2)
        new = next(t for t in w.store.tickets() if t.id != id)
        self.assertTrue(new.is_open)

    def test_stop_drops_a_launch_not_yet_done(self):
        w = self.world()
        id = w.issue()
        w.engine.schedule()
        w.comment(issue_thread(REPO, 1), "stop")
        w.settle()
        self.assertEqual(w.executor.starts, [])

    def test_comment_stopping_mid_sentence_is_not_a_stop(self):
        w = self.world()
        id = w.issue()
        w.executor.script(f"{id}-b1", FakeTurn(reports=(report("blocked", question="q?"),)))
        w.settle()
        w.executor.script(f"{id}-b1", FakeTurn(reports=(report("blocked", question="q2?"),)))
        w.comment(issue_thread(REPO, 1), "please don't stop the build on lint errors")
        w.settle()
        self.assertFalse(w.ticket(id).stopped)

    def test_hold_lets_the_turn_finish_and_starts_nothing_new(self):
        w = self.world()
        id = w.issue()
        w.executor.script(f"{id}-b1", FakeTurn(hold=True), FakeTurn(reports=(report("blocked", question="q?"),)))
        w.settle()
        w.engine.command("hold", id, "owner")
        w.comment(issue_thread(REPO, 1), "more")
        w.executor.release(f"{id}-b1-1")
        w.settle()
        self.assertEqual(w.executor.stopped, [])
        self.assertEqual(len(w.builder_texts(id)), 1)
        w.engine.command("release", id, "owner")
        w.settle()
        self.assertIn("more", w.builder_texts(id)[1])

    def test_hold_pauses_gate_actions_too(self):
        w = self.world()
        id, pr = w.to_gating(ci=CiState.GREEN)
        w.engine.command("hold", id, "owner")
        w.forge.move_main(REPO)
        w.settle()
        self.assertEqual(w.forge.updates, [], "no branch update while held")
        w.engine.command("release", id, "owner")
        w.settle()
        self.assertEqual(w.forge.updates, [(REPO, pr.number)])

    def test_a_label_on_a_tickets_thread_wakes_nothing(self):
        from lumbridge_factory.core.model import Trigger, TriggerKind

        w = self.world()
        id, pr = w.to_gating()
        turns = len(w.builder_texts(id))
        w.engine.ingest(
            Trigger("gitea", "lab", issue_thread(REPO, 1), TriggerKind.ISSUE_LABELED, "owner", repo=REPO, data={"labels": ["factory", "bug"]})
        )
        w.settle()
        self.assertEqual(len(w.builder_texts(id)), turns)
        self.assertEqual(len(w.store.tickets()), 1)

    def test_retry_clears_trouble(self):
        w = self.world()
        id = w.issue()
        w.executor.script(f"{id}-b1", FakeTurn(outcome=TurnOutcome.FAILED, error="disk full"))
        w.settle()
        t = w.ticket(id)
        self.assertEqual(t.waiting_reason, WaitReason.TROUBLE)
        self.assertIn("disk full", t.question)
        w.executor.script(f"{id}-b1", FakeTurn(reports=(report("blocked", question="q?"),)))
        w.engine.command("retry", id, "owner")
        w.settle()
        self.assertEqual(len(w.builder_texts(id)), 2)
        self.assertEqual(w.ticket(id).waiting_reason, WaitReason.QUESTION)

    def test_builder_dropped_ends_and_says_so(self):
        w = self.world()
        id = w.issue()
        w.executor.script(f"{id}-b1", FakeTurn(reports=(report("dropped", summary="already fixed"),)))
        w.settle()
        self.assertEqual(w.ticket(id).end_reason, EndReason.DROPPED)
        self.assertIn("already fixed", w.comments()[-1])


class GateScenarioTest(WorldTest):
    def test_review_comment_wakes_the_builder(self):
        w = self.world()
        id, pr = w.to_gating()
        w.executor.script(f"{id}-b1", FakeTurn(reports=(ready(pr.number),)))
        w.comment(pr_thread(REPO, pr.number), "rename this function")
        w.settle()
        self.assertIn("rename this function", w.builder_texts(id)[1])

    def test_ci_red_goes_back_to_the_builder_with_the_log_tail(self):
        w = self.world()
        id, pr = w.to_gating()
        log = "\n".join(f"line {i}" for i in range(200)) + "\nAssertionError: expected 3"
        w.forge.red(pr, log)
        w.executor.script(f"{id}-b1", w.fixes(pr, ci=CiState.GREEN))
        w.settle()
        text = w.builder_texts(id)[1]
        self.assertIn("AssertionError: expected 3", text)
        self.assertNotIn("line 10\n", text, "only the tail is inlined")
        self.assertIn("recall(", text)
        self.assertEqual(w.ticket(id).phase, Phase.READY)
        self.assertEqual(w.store.count_assignments(id, Role.REVIEWER), 1, "red never reached a reviewer")

    def test_ci_red_wakes_once_per_sha_and_doesnt_spin(self):
        w = self.world()
        id, pr = w.to_gating()
        w.forge.red(pr)
        w.executor.script(f"{id}-b1", FakeTurn(reports=(ready(pr.number),)))
        w.settle()
        self.assertEqual(len(w.builder_texts(id)), 2)
        t = w.ticket(id)
        self.assertEqual(t.waiting_reason, WaitReason.TROUBLE, "same red SHA, claimed ready: ask")

    def test_negative_review_goes_back_and_hits_the_round_limit(self):
        w = self.world()
        id, pr = w.to_gating()
        for n in range(1, 4):
            w.executor.script(f"{id}-r{n}", FakeTurn(reports=(changes(f"finding {n}"),)))
            w.executor.script(f"{id}-b1", w.fixes(pr, ci=CiState.GREEN))
        w.forge.green(pr)
        w.settle()
        self.assertEqual(w.store.count_assignments(id, Role.REVIEWER), 3, "at most 3 rounds")
        t = w.ticket(id)
        self.assertEqual((t.phase, t.waiting_reason), (Phase.WAITING, WaitReason.ROUNDS))
        self.assertEqual(t.review_rounds, 3)
        for n in range(1, 4):
            self.assertIn(f"finding {n}", w.builder_texts(id)[n])
        self.assertIn("3 review rounds", w.comments()[-1])

        # The owner's reply starts fresh rounds, and the next review can pass.
        w.executor.script(f"{id}-b1", w.fixes(pr, ci=CiState.GREEN))
        w.comment(issue_thread(REPO, 1), "fine, one more try")
        w.settle()
        t = w.ticket(id)
        self.assertEqual(t.phase, Phase.READY)
        self.assertEqual(t.review_rounds, 0)

    def test_builder_ignoring_a_review_asks_instead_of_looping(self):
        w = self.world()
        id, pr = w.to_gating()
        w.executor.script(f"{id}-r1", FakeTurn(reports=(changes(),)))
        w.executor.script(f"{id}-b1", FakeTurn(reports=(ready(pr.number),)))
        w.forge.green(pr)
        w.settle()
        t = w.ticket(id)
        self.assertEqual(t.waiting_reason, WaitReason.TROUBLE)
        self.assertIn("asked changes on", t.question)
        self.assertEqual(t.review_rounds, 1, "the same finding isn't a new round")
        self.assertEqual(len(w.builder_texts(id)), 2)
        self.assertEqual(w.store.count_assignments(id, Role.REVIEWER), 1)

    def test_main_moves_after_green_and_acceptance_rechecks(self):
        w = self.world()
        id, pr = w.to_gating()
        w.forge.green(pr)
        w.engine.tick()  # review requested, not finished
        old_sha = pr.head_sha
        w.forge.move_main(REPO)
        w.settle()
        # The update made a new head, so gates 1 and 2 restarted on it.
        self.assertEqual(w.forge.updates, [(REPO, pr.number)])
        self.assertNotEqual(pr.head_sha, old_sha)
        t = w.ticket(id)
        self.assertEqual(t.phase, Phase.GATING, "not accepted on the old SHA")
        self.assertIsNone(t.accepted_sha)
        w.forge.green(pr)
        w.settle()
        t = w.ticket(id)
        self.assertEqual((t.phase, t.accepted_sha), (Phase.READY, pr.head_sha))
        self.assertEqual(w.store.count_assignments(id, Role.REVIEWER), 2)
        self.assertEqual(t.review_rounds, 0, "a main update isn't a review round")

    def test_main_moves_after_acceptance_withdraws_ready(self):
        w = self.world()
        id, pr = w.to_gating(ci=CiState.GREEN)
        self.assertEqual(w.ticket(id).phase, Phase.READY)
        accepted = pr.head_sha
        w.forge.move_main(REPO)
        w.settle()
        t = w.ticket(id)
        self.assertEqual(t.phase, Phase.GATING)
        self.assertIsNone(t.accepted_sha)
        self.assertNotEqual(pr.head_sha, accepted)
        w.forge.green(pr)
        w.settle()
        self.assertEqual(w.ticket(id).phase, Phase.READY)

    def test_update_conflict_wakes_the_builder(self):
        w = self.world()
        id, pr = w.to_gating(ci=CiState.GREEN)
        w.forge.conflict_on_update = True
        w.forge.move_main(REPO)
        w.executor.script(f"{id}-b1", w.fixes(pr, ci=CiState.GREEN))
        w.settle()
        self.assertIn("Merge main", w.builder_texts(id)[1])
        self.assertEqual(w.ticket(id).phase, Phase.READY)

    def test_new_push_restarts_gates(self):
        w = self.world()
        id, pr = w.to_gating(ci=CiState.GREEN)
        self.assertEqual(w.ticket(id).phase, Phase.READY)
        w.forge.push(pr)  # Someone pushed; CI hasn't run
        w.settle()
        t = w.ticket(id)
        self.assertEqual(t.phase, Phase.GATING)
        self.assertIsNone(t.accepted_sha)

    def test_a_new_head_retires_the_reviewer_of_the_old_one(self):
        w = self.world(slots=1, reserved_slots=0)
        id, pr = w.to_gating()
        w.forge.green(pr)
        w.executor.script(f"{id}-r1", FakeTurn(hold=True))
        w.settle()
        self.assertEqual(w.executor.held(), [f"{id}-r1-1"])
        w.forge.push(pr, ci=CiState.GREEN)
        w.settle()
        self.assertEqual(w.executor.stopped, [f"sess-{id}-r1"])
        self.assertEqual(w.ticket(id).phase, Phase.READY, "the new reviewer got the slot and passed")
        self.assertEqual(w.ticket(id).accepted_sha, pr.head_sha)

    def test_forge_down_decides_nothing(self):
        w = self.world()
        id, pr = w.to_gating(ci=CiState.GREEN)
        before = w.ticket(id)
        w.forge.down = True
        w.settle()
        self.assertEqual(w.ticket(id), before)


class WorkerWordTest(WorldTest):
    """§1.3, §6: a worker's word is never trusted for acceptance."""

    def test_claimed_pr_that_doesnt_exist(self):
        w = self.world()
        id = w.issue()
        w.executor.script(f"{id}-b1", FakeTurn(reports=(ready(999),)), FakeTurn(reports=(ready(999),)))
        w.settle()
        t = w.ticket(id)
        self.assertIsNone(t.pr)
        self.assertEqual(t.phase, Phase.WAITING)
        self.assertIn("isn't an open PR", w.builder_texts(id)[1])

    def test_claimed_pr_from_another_branch_or_base(self):
        for branch, base in (("factory/someone-else", "main"), (None, "release")):
            with self.subTest(branch=branch, base=base):
                w = self.world()
                id = w.issue()
                pr = w.forge.open_pr(REPO, branch or f"factory/{id}", ci=CiState.GREEN, base=base)
                w.executor.script(f"{id}-b1", FakeTurn(reports=(ready(pr.number),)), FakeTurn(reports=(report("blocked", question="q?"),)))
                w.settle()
                self.assertIsNone(w.ticket(id).pr)
                self.assertIsNone(w.ticket(id).accepted_sha)

    def test_builder_claiming_a_pass_gets_nothing(self):
        w2 = self.world()
        id2 = w2.issue()
        pr2 = w2.forge.open_pr(REPO, f"factory/{id2}", ci=CiState.PENDING)
        w2.executor.script(f"{id2}-b1", FakeTurn(reports=(report("ready", pr=pr2.number, verdict="pass", summary="CI is green and reviewed"),)))
        w2.settle()
        t = w2.ticket(id2)
        self.assertEqual(t.phase, Phase.GATING)
        self.assertIsNone(t.accepted_sha)
        self.assertEqual(w2.forge.reviews.get((REPO, pr2.number), []), [], "the builder's verdict isn't posted")

    def test_review_from_the_builder_bot_or_a_person_doesnt_count(self):
        for author in ("scratch-factory", "owner", "stranger"):
            with self.subTest(author=author):
                w = self.world()
                id, pr = w.to_gating()
                w.forge.green(pr)
                w.forge.add_review(REPO, pr.number, pr.head_sha, "pass", author)
                w.executor.script(f"{id}-r1", FakeTurn(hold=True))
                w.settle()
                self.assertEqual(w.ticket(id).phase, Phase.GATING)
                self.assertEqual(w.store.count_assignments(id, Role.REVIEWER), 1)

    def test_a_pass_on_an_older_sha_doesnt_count(self):
        w = self.world()
        id, pr = w.to_gating(ci=CiState.GREEN)
        self.assertEqual(w.ticket(id).phase, Phase.READY)
        w.executor.script(f"{id}-r2", FakeTurn(hold=True))
        w.forge.push(pr, ci=CiState.GREEN)
        w.settle()
        self.assertEqual(w.ticket(id).phase, Phase.GATING, "the old pass doesn't carry over")

    def test_reviewer_verdict_is_only_what_the_forge_then_shows(self):
        w = self.world()
        id, pr = w.to_gating()
        w.forge.green(pr)
        w.executor.script(f"{id}-r1", FakeTurn(reports=(passed(),)))
        w.settle()
        review = w.forge.reviews[(REPO, pr.number)][0]
        self.assertEqual((review.sha, review.author), (pr.head_sha, "scratch-review"))

    def test_garbled_tool_calls_are_no_claims(self):
        w = self.world()
        id = w.issue()
        w.executor.script(
            f"{id}-b1",
            FakeTurn(garbage=({"seq": "x"}, {"kind": "report"}, {"seq": 9, "kind": "report", "report": {"status": "accepted"}})),
            FakeTurn(reports=(report("blocked", question="q?"),)),
        )
        w.settle()
        self.assertEqual(w.ticket(id).phase, Phase.WAITING)
        self.assertEqual(w.ticket(id).question, "q?")


class CarriedReviewWorldTest(WorldTest):
    """Opt-in: after a clean main merge only CI repeats; a real change gets a review."""

    def carry_world(self):
        from lumbridge_factory.core.policy import RepoPolicy

        return self.world(repositories=(RepoPolicy(name=REPO, required_checks=("CI / checks (pull_request)",), carry_review_across_main=True),))

    def test_a_clean_main_merge_needs_only_ci(self):
        w = self.carry_world()
        id, pr = w.to_gating(ci=CiState.GREEN)
        self.assertEqual(w.ticket(id).phase, Phase.READY)
        self.assertEqual(w.store.count_assignments(id, Role.REVIEWER), 1)
        w.forge.move_main(REPO)
        w.settle()
        self.assertEqual(w.forge.updates, [(REPO, pr.number)])
        self.assertIsNone(w.ticket(id).accepted_sha, "not before CI on the new head")
        w.forge.green(pr)
        w.settle()
        t = w.ticket(id)
        self.assertEqual((t.phase, t.accepted_sha), (Phase.READY, pr.head_sha))
        self.assertEqual(w.store.count_assignments(id, Role.REVIEWER), 1, "the pass carried: no second review")
        self.assertIn("carries", w.comments()[-1] if w.comments() else "", "the owner is told why")

    def test_a_real_change_still_gets_a_review(self):
        w = self.carry_world()
        id, pr = w.to_gating(ci=CiState.GREEN)
        w.forge.push(pr, ci=CiState.GREEN)  # a real commit: the diff against main changes
        w.settle()
        self.assertEqual(w.store.count_assignments(id, Role.REVIEWER), 2)
        self.assertEqual(w.ticket(id).accepted_sha, pr.head_sha)

    def test_without_the_flag_a_main_merge_is_reviewed_again(self):
        w = self.world()
        id, pr = w.to_gating(ci=CiState.GREEN)
        w.forge.move_main(REPO)
        w.settle()
        w.forge.green(pr)
        w.settle()
        self.assertEqual(w.store.count_assignments(id, Role.REVIEWER), 2)
