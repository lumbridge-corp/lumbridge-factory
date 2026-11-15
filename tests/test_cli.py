"""The CLI on a scratch DB, and the JSON the Factory screen will read."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from lumbridge_factory.names import NEEDS_KEY
from lumbridge_factory import cli
from lumbridge_factory.engine import Engine
from lumbridge_factory.fakes import FakeAccounts, FakeClock, FakeExecutor, FakeForge
from lumbridge_factory.store import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CliBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lumbridge-factory-cli-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = os.path.join(self.dir, "scratch.sqlite3")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--db", self.db, *argv])
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv):
        code, out, err = self.run_cli(*argv)
        self.assertEqual(code, 0, err)
        return out

    def demo(self):
        self.ok("demo")

    def _tickets(self):
        code, out, err = self.run_cli("--json", "tickets", "--all")
        self.assertEqual(code, 0, err)
        return out


class StatusTest(CliBase):
    def test_status_on_a_new_scratch_db(self):
        out = self.ok("status")
        self.assertIn("scratch: 0 tickets", out)
        self.assertIn("Nothing needs you.", out)
        self.assertTrue(os.path.exists(self.db))

    def test_status_json_snapshot_shape(self):
        snap = json.loads(self.ok("--json", "status"))
        for key in ("schema", "instance", "policy_version", "capacity", "effects", NEEDS_KEY, "working", "tickets"):
            self.assertIn(key, snap)
        self.assertEqual(snap["schema"], 1)
        self.assertEqual(snap[NEEDS_KEY], [])

    def test_a_real_process_can_run_it(self):
        env = {**os.environ, "LUMBRIDGE_FACTORY_DB": self.db}
        run = subprocess.run([os.path.join(ROOT, "bin", "lumbridge-factory"), "status"], capture_output=True, text=True, env=env, cwd=self.dir)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("0 tickets", run.stdout)

    def test_the_store_variable_under_its_name_before_0_1_0(self):
        env = {k: v for k, v in os.environ.items() if k != "LUMBRIDGE_FACTORY_DB"}
        env["FACTORY2_DB"] = old = os.path.join(self.dir, "old-name.sqlite3")
        run = subprocess.run([os.path.join(ROOT, "bin", "lumbridge-factory"), "status"], capture_output=True, text=True, env=env, cwd=self.dir)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(os.path.exists(old), "the store named by the old variable is the one used")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "factory.sqlite3")))

    def test_demo_orders_by_what_needs_the_owner(self):
        self.demo()
        snap = json.loads(self.ok("--json", "status"))
        self.assertEqual(len(snap["tickets"]), 4)
        order = [v["title"] for v in snap["tickets"]]
        self.assertEqual(order[0], "Rename the settings page", "a question comes first")
        self.assertEqual(order[1], "Dark mode toggle", "then ready to merge")
        self.assertEqual(order[2], "Add usage graphs", "then working")
        self.assertEqual(order[3], "Fix the flaky test", "then the rest")
        self.assertEqual(snap["capacity"]["used"] + snap["capacity"]["used_reserved"], 1)
        text = self.ok("status")
        self.assertIn("Should the old URL keep working?", text)
        self.assertIn("ready to merge", text)
        self.assertIn("Working:", text)

    def test_snapshot_has_no_secrets_or_issue_bodies(self):
        self.demo()
        raw = self.ok("--json", "status")
        self.assertNotIn("Please:", raw)


class TicketCommandsTest(CliBase):
    def setUp(self):
        super().setUp()
        self.demo()
        self.views = json.loads(self._tickets())
        self.waiting = next(v for v in self.views if v["phase"] == "waiting")["id"]

    def test_tickets_and_show(self):
        self.assertIn("Dark mode toggle", self.ok("tickets"))
        out = self.ok("show", self.waiting)
        self.assertIn("mandate: explicit, asked by owner", out)
        self.assertIn("subscriptions:", out)
        view = json.loads(self.ok("--json", "show", self.waiting))
        self.assertEqual(view["reports"][0]["question"], "Should the old URL keep working?")
        self.assertEqual(view["assignments"][0]["role"], "builder")

    def test_show_unknown_ticket(self):
        code, _, err = self.run_cli("show", "t-nope")
        self.assertEqual(code, 1)
        self.assertIn("No ticket", err)

    def test_stop_hold_retry_on_scratch(self):
        self.assertIn("done", self.ok("hold", self.waiting))
        self.assertTrue(json.loads(self.ok("--json", "show", self.waiting))["held"])
        self.ok("hold", self.waiting, "--release")
        self.assertFalse(json.loads(self.ok("--json", "show", self.waiting))["held"])
        self.ok("retry", self.waiting)
        self.assertIsNone(json.loads(self.ok("--json", "show", self.waiting))["question"])
        self.ok("stop", self.waiting)
        view = json.loads(self.ok("--json", "show", self.waiting))
        self.assertTrue(view["stopped"])
        self.assertEqual(view["end_reason"], "stopped")

    def test_stop_an_unknown_ticket_fails(self):
        code, _, err = self.run_cli("stop", "t-nope")
        self.assertEqual(code, 1)
        self.assertIn("No ticket", err)

    def test_while_factoryd_holds_the_lock_commands_spool_and_apply_once(self):
        clock, forge, executor = FakeClock(), FakeForge(), FakeExecutor()
        store = Store.writer(self.db)
        self.addCleanup(store.close)
        engine = Engine(
            store, executor=executor, gates=forge, channel=forge, accounts=FakeAccounts(), clock=clock, spool_dir=self.db + ".commands"
        )
        self.assertIn("queued for factoryd", self.ok("hold", self.waiting))
        self.assertFalse(store.ticket(self.waiting).held, "only the writer writes")
        files = os.listdir(self.db + ".commands")
        self.assertEqual(len(files), 1)
        self.assertEqual(engine.drain_spool(), 1)
        self.assertTrue(store.ticket(self.waiting).held)
        self.assertEqual(os.listdir(self.db + ".commands"), [])
        self.assertEqual(engine.drain_spool(), 0)

    def test_crash_after_applying_a_spooled_command_replays_harmlessly(self):
        from lumbridge_factory.engine import spool_command

        store = Store.writer(self.db)
        self.addCleanup(store.close)
        engine = Engine(store, clock=FakeClock(), spool_dir=self.db + ".commands")
        path = spool_command(self.db + ".commands", {"command": "hold", "ticket": self.waiting, "actor": "cli:k"})
        with open(path) as f:
            saved = f.read()
        engine.drain_spool()
        with open(path, "w") as f:
            f.write(saved)
        self.assertEqual(engine.drain_spool(), 1)
        self.assertTrue(store.ticket(self.waiting).held)

    def test_a_bad_spool_file_is_set_aside_and_the_factory_goes_on(self):
        from lumbridge_factory.engine import spool_command

        store = Store.writer(self.db)
        self.addCleanup(store.close)
        engine = Engine(store, clock=FakeClock(), spool_dir=self.db + ".commands")
        spool = self.db + ".commands"
        os.makedirs(spool)
        with open(os.path.join(spool, "0-junk.json"), "w") as f:
            f.write("{not json")
        spool_command(spool, {"command": "hold", "ticket": "t-nope", "actor": "cli:k"})
        spool_command(spool, {"command": "policy", "rule": "build", "mode": "off", "actor": "cli:k"})
        spool_command(spool, {"command": "hold", "ticket": self.waiting, "actor": "cli:k"})
        self.assertEqual(engine.drain_spool(), 4)
        self.assertTrue(store.ticket(self.waiting).held)
        self.assertEqual(len(os.listdir(spool)), 3)
        self.assertTrue(all(n.endswith(".rejected") for n in os.listdir(spool)))
        self.assertEqual(engine.drain_spool(), 0)


class PolicyCommandsTest(CliBase):
    def test_show_set_mode_and_allow(self):
        self.ok("status")
        out = self.ok("policy")
        self.assertIn("gitea: owner", out)
        self.assertIn("main-red: observe", out)
        self.ok("policy", "set-mode", "main-red", "auto")
        data = json.loads(self.ok("--json", "policy"))
        self.assertEqual(data["policy"]["rules"]["main-red"], "auto")
        self.assertEqual(data["policy"]["version"], 2)
        self.assertEqual(len(data["history"]), 2)
        self.ok("policy", "allow", "gitea", "owner", "alex")
        self.assertIn("alex", json.loads(self.ok("--json", "policy"))["policy"]["allowed"]["gitea"])

    def test_policy_refuses_turning_off_build(self):
        self.ok("status")
        code, _, err = self.run_cli("policy", "set-mode", "build", "off")
        self.assertEqual(code, 2)
        self.assertIn("always on", err)

    def test_policy_is_refused_at_the_cli_even_when_factoryd_holds_the_lock(self):
        self.ok("status")
        store = Store.writer(self.db)
        self.addCleanup(store.close)
        code, _, err = self.run_cli("policy", "set-mode", "build", "off")
        self.assertEqual(code, 2)
        self.assertIn("always on", err)
        self.assertFalse(os.path.exists(self.db + ".commands"))

    def test_policy_refuses_emptying_the_allowed_list(self):
        self.ok("status")
        self.ok("policy", "allow", "office")
        code, _, err = self.run_cli("policy", "allow", "gitea")
        self.assertEqual(code, 2)
        self.assertIn("who may ask", err)


class CarryReviewPolicyTest(unittest.TestCase):
    def test_turning_it_on_is_a_new_policy_version_for_that_repo_only(self):
        import tempfile, os
        from lumbridge_factory import cli
        from lumbridge_factory.store import Store

        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "f.sqlite3")
            self.assertEqual(cli.main(["--db", db, "policy", "carry-review", "acme/sandbox", "on"]), 0)
            store = Store.reader(db)
            try:
                policy = store.policy()
            finally:
                store.close()
            self.assertEqual(policy.version, 2)
            self.assertTrue(policy.repo("acme/sandbox").carry_review_across_main)
            self.assertNotEqual(cli.main(["--db", db, "policy", "carry-review", "no/such", "on"]), 0, "an unknown repo is refused")


class CiRunnersPolicyTest(unittest.TestCase):
    def test_several_runners_for_one_repo(self):
        import tempfile, os
        from lumbridge_factory import cli
        from lumbridge_factory.store import Store

        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "f.sqlite3")
            self.assertEqual(cli.main(["--db", db, "policy", "ci-runners", "acme/sandbox", "r1", "r2"]), 0)
            store = Store.reader(db)
            try:
                self.assertEqual(store.policy().repo("acme/sandbox").ci_runner, "r1,r2")
            finally:
                store.close()
            self.assertNotEqual(cli.main(["--db", db, "policy", "ci-runners", "acme/sandbox", "a,b"]), 0, "one name per argument")


class RepositoriesPolicyTest(unittest.TestCase):
    """Repositories are added and removed on a live store."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.db = os.path.join(self.dir.name, "f.sqlite3")

    def policy(self):
        from lumbridge_factory.store import Store
        store = Store.reader(self.db)
        try:
            return store.policy()
        finally:
            store.close()

    def cli(self, *argv):
        from lumbridge_factory import cli
        return cli.main(["--db", self.db, *argv])

    def test_adding_a_repo_is_a_new_version_and_keeps_the_others(self):
        self.assertEqual(self.cli("policy", "add-repo", "acme/gadgets", "--base", "acme", "--runner", "r21",
                                  "--runner", "r22", "--data-rules", "Throwaway WIDGETS_HOME only."), 0)
        policy = self.policy()
        self.assertEqual(policy.version, 2)
        self.assertIsNotNone(policy.repo("acme/sandbox"))
        repo = policy.repo("acme/gadgets")
        self.assertEqual((repo.base, repo.ci_runner, repo.required_checks), ("acme", "r21,r22", ("CI / checks (pull_request)",)))
        self.assertEqual(repo.data_rules, "Throwaway WIDGETS_HOME only.")
        self.assertFalse(repo.carry_review_across_main)

    def test_adding_is_refused_twice_without_a_runner_or_with_a_bad_name(self):
        self.assertEqual(self.cli("policy", "add-repo", "acme/widgets", "--runner", "r11"), 0)
        self.assertNotEqual(self.cli("policy", "add-repo", "acme/widgets", "--runner", "r11"), 0, "already there")
        self.assertNotEqual(self.cli("policy", "add-repo", "acme/x", "--runner", " "), 0, "no runner named")
        self.assertNotEqual(self.cli("policy", "add-repo", "no-owner", "--runner", "r"), 0, "owner/name")
        self.assertNotEqual(self.cli("policy", "add-repo", "acme/y", "--base", " ", "--runner", "r"), 0, "a base")
        self.assertEqual(self.policy().version, 2)

    def test_adding_refuses_what_could_never_match_the_forge(self):
        self.assertNotEqual(self.cli("policy", "add-repo", "Acme/Sandbox", "--runner", "r"), 0, "the same repo in another case")
        self.assertNotEqual(self.cli("policy", "add-repo", "../x", "--runner", "r"), 0, "not a name")
        self.assertNotEqual(self.cli("policy", "add-repo", "acme/a b", "--runner", "r"), 0, "not a name")
        self.assertNotEqual(self.cli("policy", "add-repo", "acme/y", "--base", "ma in", "--runner", "r"), 0, "a base with a space")
        self.assertNotEqual(self.cli("policy", "add-repo", "acme/y", "--runner", "r", "--check", ""), 0, "an empty check")
        self.assertNotEqual(self.cli("policy", "add-repo", "acme/y", "--runner", "r\t1"), 0, "a tab in a runner name")
        self.assertEqual(self.policy().version, 1)
        self.assertEqual(self.cli("policy", "add-repo", "acme-org/lumbridge.mods_2", "--runner", "r"), 0)

    def test_data_rules_and_removal(self):
        self.assertEqual(self.cli("policy", "add-repo", "acme/widgets", "--runner", "r11"), 0)
        self.assertEqual(self.cli("policy", "data-rules", "acme/widgets", "Scratch cores only."), 0)
        self.assertEqual(self.policy().repo("acme/widgets").data_rules, "Scratch cores only.")
        self.assertNotEqual(self.cli("policy", "data-rules", "no/such", "x"), 0)
        self.assertEqual(self.cli("policy", "remove-repo", "acme/widgets"), 0)
        self.assertIsNone(self.policy().repo("acme/widgets"))
        self.assertNotEqual(self.cli("policy", "remove-repo", "acme/sandbox"), 0, "the last repository can't go")

    def test_removal_is_refused_while_a_ticket_is_open_there(self):
        from lumbridge_factory.core.policy import PolicyError
        from lumbridge_factory.engine import check_removal
        from lumbridge_factory.core.model import Ticket

        class FakeStore:
            def tickets(self, status=None):
                return [Ticket(id="t-1", repo="acme/widgets", base="main", branch="factory/t-1", title="x",
                               origin_thread="i", mandate_id="m-1", requested_by="owner")]

        with self.assertRaises(PolicyError):
            check_removal(FakeStore(), {"command": "policy", "remove_repo": "acme/widgets"})
        check_removal(FakeStore(), {"command": "policy", "remove_repo": "acme/other"})

    def test_an_older_release_can_read_a_policy_without_repo_data_rules(self):
        from lumbridge_factory.core.policy import RepoPolicy
        self.assertEqual(self.cli("policy", "add-repo", "acme/widgets", "--runner", "r11"), 0)
        stored = self.policy().to_dict()
        for r in stored["repositories"]:
            self.assertNotIn("data_rules", r, "an empty one isn't stored")
            old_fields = {f for f in RepoPolicy.__dataclass_fields__ if f != "data_rules"}
            self.assertLessEqual(set(r), old_fields)

    def test_a_repos_data_rules_reach_only_its_own_tickets(self):
        from lumbridge_factory.core import prompts
        self.assertEqual(self.cli("policy", "add-repo", "acme/widgets", "--runner", "r11", "--data-rules", "No real widgets-core."), 0)
        policy = self.policy()
        self.assertIn("Data in acme/widgets: No real widgets-core.", prompts.instance_rules(policy, "acme/widgets"))
        self.assertNotIn("No real widgets-core", prompts.instance_rules(policy, "acme/sandbox"))
        self.assertNotIn("No real widgets-core", prompts.instance_rules(policy))
