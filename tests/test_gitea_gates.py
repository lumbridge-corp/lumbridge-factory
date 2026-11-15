"""§1.3, §5: what the host reads from the forge, and what it writes there. Never a worker's word."""

import os
import shutil
import tempfile
import time
import unittest
from dataclasses import replace

from fake_gitea import REPO, FakeGitea, sha_of, stamp

from lumbridge_factory.channels.gitea import GiteaChannel, disarm, has_footer, parse_verdict, with_footer
from lumbridge_factory.names import TAG
from lumbridge_factory.core.facts import CiState, PrState, ReviewState
from lumbridge_factory.core.policy import RepoPolicy, default_policy
from lumbridge_factory.gates import GiteaGates
from lumbridge_factory.gitea_api import GiteaClient, GiteaError
from lumbridge_factory.ports import CannotCheck

CHECK = "CI / checks (pull_request)"
REQUIRED = (CHECK,)
POLICY = replace(
    default_policy("sandbox", REPO),
    repositories=(RepoPolicy(REPO, required_checks=REQUIRED),),
    bots=("factory-bot",),
    reviewer_bots=("review-bot",),
)


class GiteaCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lumbridge-factory-gitea-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.fake = FakeGitea()
        self.addCleanup(self.fake.close)
        self.client = self.client_for("factory-token")
        self.review_client = self.client_for("review-token")
        self.gates = GiteaGates(self.client, lambda: POLICY)
        self.channel = GiteaChannel(self.client, self.review_client)

    def client_for(self, token: str) -> GiteaClient:
        path = os.path.join(self.dir, token)
        with open(path, "w") as fh:
            fh.write(token + "\n")
        return GiteaClient(self.fake.url, path, timeout=5)


class CiGateTests(GiteaCase):
    def test_green_only_on_the_exact_sha(self):
        old, new = sha_of("old"), sha_of("new")
        self.fake.status(old, "success")
        self.assertEqual(self.gates.ci(REPO, new, REQUIRED, "").state, CiState.PENDING)
        self.fake.status(new, "success")
        facts = self.gates.ci(REPO, new, REQUIRED, "")
        self.assertEqual((facts.state, facts.sha), (CiState.GREEN, new))

    def test_only_the_required_context_counts(self):
        sha = sha_of("x")
        self.fake.status(sha, "success", context="CI / other (pull_request)")
        self.assertEqual(self.gates.ci(REPO, sha, REQUIRED, "").state, CiState.PENDING)
        self.fake.status(sha, "failure", context="CI / other (pull_request)")
        self.assertEqual(self.gates.ci(REPO, sha, REQUIRED, "").state, CiState.PENDING, "a non-required failure isn't red either")

    def test_every_required_context_must_be_green(self):
        sha = sha_of("x")
        both = (CHECK, "CI / lint (pull_request)")
        self.fake.status(sha, "success")
        self.assertEqual(self.gates.ci(REPO, sha, both, "").state, CiState.PENDING)
        self.fake.status(sha, "success", context="CI / lint (pull_request)")
        self.assertEqual(self.gates.ci(REPO, sha, both, "").state, CiState.GREEN)

    def test_the_newest_status_of_a_context_wins(self):
        sha = sha_of("x")
        self.fake.status(sha, "failure", description="first")
        self.fake.status(sha, "success")
        self.assertEqual(self.gates.ci(REPO, sha, REQUIRED, "").state, CiState.GREEN)
        self.fake.status(sha, "pending")
        self.assertEqual(self.gates.ci(REPO, sha, REQUIRED, "").state, CiState.PENDING)

    def test_red_carries_its_description(self):
        sha = sha_of("x")
        self.fake.status(sha, "failure", description="CHANGES.md needs an entry")
        facts = self.gates.ci(REPO, sha, REQUIRED, "")
        self.assertEqual(facts.state, CiState.RED)
        self.assertEqual(facts.failures[0].context, CHECK)
        self.assertIn("CHANGES.md", facts.failures[0].log_tail)

    def test_red_prefers_the_job_log(self):
        sha = sha_of("x")
        self.fake.status(sha, "failure", description="short")
        self.fake.job(sha, conclusion="failure", log="line\nthe real failure")
        self.assertIn("the real failure", self.gates.ci(REPO, sha, REQUIRED, "").failures[0].log_tail)

    def test_a_named_runner_must_be_the_one_that_ran_it(self):
        sha = sha_of("x")
        self.fake.status(sha, "success")
        self.assertEqual(self.gates.ci(REPO, sha, REQUIRED, "r1").state, CiState.PENDING, "no job to prove it")
        self.fake.job(sha, runner="someone-else")
        self.assertEqual(self.gates.ci(REPO, sha, REQUIRED, "r1").state, CiState.PENDING)
        self.fake.job(sha, runner="r1")
        self.assertEqual(self.gates.ci(REPO, sha, REQUIRED, "r1").state, CiState.GREEN)

    def test_unknown_commit_is_pending_not_an_error(self):
        self.assertEqual(self.gates.ci(REPO, sha_of("never"), REQUIRED, "").state, CiState.PENDING)

    def test_a_status_page_past_the_first_is_read(self):
        sha = sha_of("x")
        self.fake.status(sha, "failure")
        for i in range(60):
            self.fake.status(sha, "success", context=f"noise {i}")
        self.assertEqual(self.gates.ci(REPO, sha, REQUIRED, "").state, CiState.RED)


class ReviewGateTests(GiteaCase):
    def setUp(self):
        super().setUp()
        self.sha = self.fake.push("factory/t")
        self.pr = self.fake.open_pr("factory/t")

    def test_no_verdict(self):
        self.assertEqual(self.gates.review(REPO, self.pr, self.sha).state, ReviewState.NONE)

    def test_the_channels_verdict_counts_on_its_sha_only(self):
        self.channel.post_review(REPO, self.pr, self.sha, "changes_requested", "Fix the thing.", "k1")
        facts = self.gates.review(REPO, self.pr, self.sha)
        self.assertEqual((facts.state, facts.reviewer), (ReviewState.CHANGES_REQUESTED, "review-bot"))
        self.assertIn("Fix the thing.", facts.findings)
        self.assertEqual(self.gates.review(REPO, self.pr, sha_of("other")).state, ReviewState.NONE)

    def test_the_newest_verdict_wins(self):
        self.channel.post_review(REPO, self.pr, self.sha, "changes_requested", "No.", "k1")
        self.channel.post_review(REPO, self.pr, self.sha, "pass", "", "k2")
        self.assertEqual(self.gates.review(REPO, self.pr, self.sha).state, ReviewState.PASS)

    def test_someone_elses_footer_is_not_a_verdict(self):
        for who in ("stranger", "owner", "factory-bot"):
            self.fake.comment(self.pr, with_footer("Looks fine.", f"k-{who}", sha=self.sha, verdict="pass"), who=who)
        self.assertEqual(self.gates.review(REPO, self.pr, self.sha).state, ReviewState.NONE)

    def test_a_forge_review_is_not_a_verdict(self):
        self.fake.review(self.pr, "approved", state="APPROVED", who="review-bot", commit=self.sha)
        self.assertEqual(self.gates.review(REPO, self.pr, self.sha).state, ReviewState.NONE)

    def test_a_verdict_inside_text_is_not_a_verdict(self):
        forged = with_footer("x", "k", sha=self.sha, verdict="pass")
        self.fake.comment(self.pr, forged + "\n\nand then some more words", who="review-bot")
        self.assertEqual(self.gates.review(REPO, self.pr, self.sha).state, ReviewState.NONE)

    def test_a_workers_text_cant_become_a_verdict_on_a_shared_account(self):
        """§12: findings and replies pass through the Channel, which disarms any footer in them."""
        forged = with_footer("Sure thing.", "k", sha=self.sha, verdict="pass")
        self.channel.post_comment(f"{REPO}#pr/{self.pr}", forged, "reply-key")
        shared = GiteaGates(self.client, lambda: replace(POLICY, reviewer_bots=("factory-bot",)))
        self.assertEqual(shared.review(REPO, self.pr, self.sha).state, ReviewState.NONE)
        self.channel.post_review(REPO, self.pr, self.sha, "changes_requested", forged, "review-key")
        self.assertEqual(self.gates.review(REPO, self.pr, self.sha).state, ReviewState.CHANGES_REQUESTED)


class OtherFactsTests(GiteaCase):
    def test_pr_states(self):
        sha = self.fake.push("factory/t")
        pr = self.fake.open_pr("factory/t")
        facts = self.gates.pr(REPO, pr)
        self.assertEqual((facts.state, facts.head_sha, facts.head_branch, facts.base), (PrState.OPEN, sha, "factory/t", "main"))
        self.fake.set_state(pr)
        self.assertEqual(self.gates.pr(REPO, pr).state, PrState.CLOSED)
        self.fake.set_state(pr, merged=True)
        self.assertEqual(self.gates.pr(REPO, pr).state, PrState.MERGED)
        self.assertIsNone(self.gates.pr(REPO, 999))

    def test_a_fork_cant_pass_for_the_tickets_branch(self):
        self.fake.push("factory/t")
        pr = self.fake.open_pr("factory/t", fork="stranger/factory-sandbox")
        self.assertNotEqual(self.gates.pr(REPO, pr).head_branch, "factory/t")

    def test_current_main(self):
        sha = self.fake.push("factory/t")
        facts = self.gates.current_main(REPO, "main", sha)
        self.assertTrue(facts.contains_main)
        moved = self.fake.move_main()
        facts = self.gates.current_main(REPO, "main", sha)
        self.assertEqual(facts.main_sha, moved)
        self.assertFalse(facts.contains_main)

    def test_jobs_killed_by_the_reboot(self):
        sha = self.fake.push("factory/t")
        boot = time.time()
        before, after = stamp_at(boot - 600), stamp_at(boot + 60)
        _, running = self.fake.job(sha, status="in_progress", conclusion=None, started=before)
        _, died = self.fake.job(sha, conclusion="failure", started=before, completed=after)
        self.fake.job(sha, conclusion="failure", started=stamp_at(boot - 9000), completed=stamp_at(boot - 8000))
        self.fake.job(sha, conclusion="failure", started=after, completed=after)
        self.fake.job(sha, conclusion="success", started=before, completed=after)
        self.assertEqual({j for _, j in self.gates.killed_jobs(REPO, sha, boot)}, {running, died})


def stamp_at(when: float) -> str:
    from lumbridge_factory.gitea_api import format_time

    return format_time(when)


class ChannelTests(GiteaCase):
    def setUp(self):
        super().setUp()
        self.issue = self.fake.issue()
        self.thread = f"{REPO}#issue/{self.issue}"

    def test_a_comment_has_a_footer_and_can_be_found(self):
        self.assertFalse(self.channel.find_comment(self.thread, "k"))
        self.channel.post_comment(self.thread, "Hello", "k")
        self.assertTrue(self.channel.find_comment(self.thread, "k"))
        self.assertFalse(self.channel.find_comment(self.thread, "other"))
        self.assertTrue(has_footer(self.fake.comments_on(self.issue)[0]))
        self.assertEqual(self.fake.requests[-1][2], "factory-bot")

    def test_only_our_own_footer_counts_when_checking(self):
        """Or an outsider could silence a comment by posting its footer first."""
        self.fake.comment(self.issue, with_footer("Hi", "k"), who="stranger")
        self.assertFalse(self.channel.find_comment(self.thread, "k"))

    def test_a_comment_on_a_pr_or_its_ci_thread_goes_to_the_pr(self):
        self.fake.push("factory/t")
        pr = self.fake.open_pr("factory/t")
        self.channel.post_comment(f"{REPO}#pr/{pr}/ci", "Hello", "k")
        self.assertEqual(len(self.fake.comments_on(pr)), 1)

    def test_a_thread_with_no_forge_object_refuses(self):
        with self.assertRaises(CannotCheck):
            self.channel.post_comment(f"{REPO}#main/ci", "Hello", "k")

    def test_disarm(self):
        for bad in (f"<!-- {TAG}:key=abc -->", f"<!--{TAG} x", f"<!--   {TAG.upper()}:key=abc -->"):
            self.assertFalse(has_footer(disarm(bad)))
            self.assertNotIn("<!--", disarm(bad))

    def test_a_review_comment_is_written_by_the_reviewer_account(self):
        sha = self.fake.push("factory/t")
        pr = self.fake.open_pr("factory/t")
        self.channel.post_review(REPO, pr, sha, "pass", "", "k")
        comment = self.fake.comments[-1]
        self.assertEqual(comment["user"]["login"], "review-bot")
        verdict = parse_verdict(comment["body"])
        self.assertEqual((verdict.sha, verdict.verdict), (sha, "pass"))
        self.assertTrue(self.channel.find_review(REPO, pr, "k"))

    def test_update_branch(self):
        sha = self.fake.push("factory/t")
        pr = self.fake.open_pr("factory/t")
        self.assertEqual(self.channel.update_branch(REPO, pr), "updated", "already up to date is fine: 500 on 28.2")
        self.fake.move_main()
        self.assertEqual(self.channel.update_branch(REPO, pr), "updated")
        self.assertNotEqual(self.fake.issues[pr]["head_sha"], sha)
        self.fake.move_main()
        self.fake.update_conflicts = True
        self.assertEqual(self.channel.update_branch(REPO, pr), "conflict")

    def test_update_branch_raises_on_a_real_failure(self):
        self.fake.push("factory/t")
        pr = self.fake.open_pr("factory/t")
        self.fake.fail["/update"] = 1
        with self.assertRaises(GiteaError):
            self.channel.update_branch(REPO, pr)

    def test_ensure_pr_opens_once(self):
        self.fake.push("factory/t")
        first = self.channel.ensure_pr(REPO, "factory/t", "main", "Title", "Body", "k")
        again = self.channel.ensure_pr(REPO, "factory/t", "main", "Title", "Body", "k")
        self.assertEqual(first, again)
        self.assertEqual(self.fake.count("POST", "/pulls"), 1)
        self.assertEqual(self.fake.issues[first["pr"]]["user"]["login"], "factory-bot")

    def test_ensure_pr_with_nothing_to_open(self):
        self.assertIn("does not exist", self.channel.ensure_pr(REPO, "factory/none", "main", "T", "B", "k")["refused"])
        self.fake.push("factory/same", self.fake.main_sha())
        self.assertIn("refused", self.channel.ensure_pr(REPO, "factory/same", "main", "T", "B", "k"))

    def test_rerun(self):
        self.channel.rerun_job(REPO, 11, 22)
        self.assertEqual(self.fake.reruns, [22])


class ClientTests(GiteaCase):
    def test_paged_reads_get_everything(self):
        issue = self.fake.issue()
        for i in range(120):
            self.fake.comment(issue, f"c{i}")
        self.assertEqual(len(list(self.client.pages(f"/repos/{REPO}/issues/{issue}/comments"))), 120)

    def test_no_token_in_an_error(self):
        self.fake.down = True
        with self.assertRaises(GiteaError) as caught:
            self.client.get(f"/repos/{REPO}")
        self.assertNotIn("factory-token", str(caught.exception))

    def test_a_redirect_is_not_followed(self):
        """urllib would otherwise carry the Authorization header to wherever it points."""
        other = FakeGitea()
        self.addCleanup(other.close)
        self.fake.redirect_to = other.url + "/api/v1/user"
        with self.assertRaises(GiteaError):
            self.client.login()
        self.assertEqual(other.requests, [])

    def test_the_token_file_is_read_each_time(self):
        self.assertEqual(self.client.login(), "factory-bot")
        with open(self.client.token_file, "w") as fh:
            fh.write("owner-token\n")
        self.assertEqual(self.client.login(), "owner")

    def test_unreachable_is_a_gitea_error(self):
        path = self.client.token_file
        with self.assertRaises(GiteaError) as caught:
            GiteaClient("http://127.0.0.1:9", path, timeout=1).login()
        self.assertIsNone(caught.exception.status)

class FailureExcerptTest(unittest.TestCase):
    def test_what_failed_is_at_the_end_where_a_card_keeps_it(self):
        from lumbridge_factory.core.cards import log_card
        from lumbridge_factory.gates import failure_excerpt

        passes = "\n".join(f"2026-11-04T12:02:22.27Z PASS  check {i}" for i in range(200))
        log = "2026-11-04T12:00:00.00Z FAIL  ios-light: no page errors  [dimensions]\n" + passes
        card = log_card(failure_excerpt(log), author="ci", kind="ci_log")
        self.assertIn("ios-light: no page errors", card)
        self.assertIn("1 of 1", card)

    def test_a_log_with_no_such_line_is_just_its_end(self):
        from lumbridge_factory.gates import failure_excerpt

        self.assertEqual(failure_excerpt("all fine\nexit 1"), "all fine\nexit 1")




class ChangeFingerprintReaderTest(unittest.TestCase):
    """GiteaGates.change: the diff counts only if the head was the same before and after reading it."""

    class Client:
        def __init__(self, heads, diff="diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n", fail=False):
            self.heads, self.diff, self.fail = list(heads), diff, fail

        def get_or_none(self, path, **q):
            return {"head": {"sha": self.heads.pop(0)}}

        def text(self, path):
            if self.fail:
                from lumbridge_factory.gitea_api import GiteaError
                raise GiteaError(404, "no")
            assert path.endswith("/pulls/7.diff"), path
            return self.diff

    def change(self, client, sha="s1"):
        from lumbridge_factory.gates import GiteaGates, change_fingerprint
        from lumbridge_factory.core.policy import default_policy

        return GiteaGates(client, default_policy).change("a/b", 7, sha), change_fingerprint

    def test_a_steady_head_gives_the_fingerprint(self):
        got, fp = self.change(self.Client(["s1", "s1"]))
        self.assertEqual(got, fp(self.Client([]).diff))

    def test_a_head_that_moved_gives_nothing(self):
        self.assertIsNone(self.change(self.Client(["s1", "s2"]))[0])
        self.assertIsNone(self.change(self.Client(["s0", "s1"]))[0])

    def test_no_diff_or_an_error_gives_nothing(self):
        self.assertIsNone(self.change(self.Client(["s1", "s1"], diff=" \n"))[0])
        self.assertIsNone(self.change(self.Client(["s1", "s1"], fail=True))[0])


class TrustedRunnersTest(unittest.TestCase):
    """prod runs up to three CI runners; a green from runner 2 read as pending for ever (#13, run 12738)."""

    def test_one_or_several_names(self):
        from lumbridge_factory.gates import trusted_runners

        self.assertEqual(trusted_runners("runner-ci"), {"runner-ci"})
        self.assertEqual(trusted_runners("a, b,c ,"), {"a", "b", "c"})
        self.assertEqual(trusted_runners(""), set())

    def test_a_green_counts_only_from_a_listed_runner(self):
        from lumbridge_factory.core.policy import default_policy
        from lumbridge_factory.gates import GiteaGates

        class Client:
            def __init__(self, runner):
                self.runner = runner

            def get(self, path, **q):
                if path.endswith("/actions/runs"):
                    return {"workflow_runs": [{"id": 1}]}
                return {"jobs": [{"name": "checks", "head_sha": "s", "conclusion": "success", "runner_name": self.runner}]}

        def counts(runner, policy_runner):
            return GiteaGates(Client(runner), default_policy)._from_runner("a/b", "s", "CI / checks (pull_request)", policy_runner)

        listed = "runner-ci,runner-ci-2,runner-ci-3"
        self.assertTrue(counts("runner-ci-2", listed))
        self.assertTrue(counts("runner-ci", listed))
        self.assertFalse(counts("runner-ci-9", listed), "a runner nobody listed")
        self.assertFalse(counts("runner-ci-2", "runner-ci"), "one name means only that one")
