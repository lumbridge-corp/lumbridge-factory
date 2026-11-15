"""§7: which account takes a turn, the built-in list and an add-on's provider."""

import os
import sys
import tempfile
import textwrap
import unittest
from dataclasses import replace

from lumbridge_factory.accounts import LIMIT_REST_SECONDS, ProviderError, StaticAccounts, load_provider
from lumbridge_factory.core.executor import Role

NOW = 1_000_000.0


class StaticAccountsTest(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.accounts = StaticAccounts(["a", "b"], lambda: self.now)

    def test_the_list_order(self):
        self.assertEqual(self.accounts.pick(Role.BUILDER), "a")

    def test_a_reviewer_avoids_the_builders_account(self):
        self.assertEqual(self.accounts.pick(Role.REVIEWER, avoid="a"), "b")

    def test_a_reviewer_shares_the_builders_account_rather_than_waiting(self):
        self.accounts.mark_limited("a")
        self.assertEqual(self.accounts.pick(Role.REVIEWER, avoid="b"), "b")

    def test_a_limited_account_rests_then_comes_back(self):
        self.accounts.mark_limited("a")
        self.assertEqual(self.accounts.pick(Role.BUILDER), "b")
        self.now += LIMIT_REST_SECONDS
        self.assertEqual(self.accounts.pick(Role.BUILDER), "a")

    def test_every_account_resting_means_wait(self):
        self.accounts.mark_limited("a")
        self.accounts.mark_limited("b")
        self.assertIsNone(self.accounts.pick(Role.BUILDER))


PLUGIN = textwrap.dedent(
    '''
    from lumbridge_factory.accounts import StaticAccounts

    class Reversed(StaticAccounts):
        def __init__(self, names, clock, used=None):
            super().__init__(list(reversed(names)), clock)
            self.used = used or {}

        def used_pct(self, name):
            return self.used.get(name)

    def make(names, clock, **options):
        return Reversed(names, clock, **options)

    class Nothing:
        def __init__(self, names, clock):
            pass

    namespace = type("namespace", (), {"make": staticmethod(make)})
    '''
)


class ProviderTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.module = f"accounts_addon_{id(self)}"
        with open(os.path.join(d.name, self.module + ".py"), "w") as fh:
            fh.write(PLUGIN)
        sys.path.insert(0, d.name)
        self.addCleanup(sys.path.remove, d.name)
        self.addCleanup(sys.modules.pop, self.module, None)

    def test_no_provider_is_the_static_list(self):
        router = load_provider("", ["a", "b"])
        self.assertIsInstance(router, StaticAccounts)
        self.assertEqual(router.pick(Role.BUILDER), "a")

    def test_options_without_a_provider_are_refused(self):
        with self.assertRaisesRegex(ProviderError, "without accounts_provider"):
            load_provider("", ["a"], {"x": 1})

    def test_an_addon_builds_the_router_with_its_options(self):
        router = load_provider(f"{self.module}:make", ["a", "b"], {"used": {"b": 12.5}}, lambda: NOW)
        self.assertEqual(router.pick(Role.BUILDER), "b")
        self.assertEqual(router.used_pct("b"), 12.5)
        self.assertEqual(router.clock(), NOW)

    def test_a_class_works_as_the_target_and_dotted_attributes_resolve(self):
        self.assertEqual(load_provider(f"{self.module}:Reversed", ["a", "b"]).pick(Role.BUILDER), "b")
        self.assertEqual(load_provider(f"{self.module}:namespace.make", ["a", "b"]).pick(Role.BUILDER), "b")

    def test_what_cant_load_is_a_provider_error(self):
        for spec in ("no_colon", ":make", f"{self.module}:", "no_such_module_here:make", f"{self.module}:missing"):
            with self.subTest(spec=spec), self.assertRaises(ProviderError):
                load_provider(spec, ["a"])

    def test_unknown_options_are_a_provider_error(self):
        with self.assertRaisesRegex(ProviderError, "refused its settings"):
            load_provider(f"{self.module}:make", ["a"], {"nonsense": 1})

    def test_a_router_without_pick_is_refused(self):
        with self.assertRaisesRegex(ProviderError, "pick, mark_limited"):
            load_provider(f"{self.module}:Nothing", ["a"])

    def test_factoryd_reports_a_bad_provider_as_a_config_error(self):
        from lumbridge_factory.factoryd import Config, ConfigError, build_engine
        from lumbridge_factory.store import Store

        d = tempfile.mkdtemp()
        config = Config(
            db=os.path.join(d, "f.sqlite3"), socket="s", gitea_url="http://example.invalid", token_file="t", review_token_file="r",
            accounts=("a",), accounts_provider="no_such_module_here:make",
        )
        store = Store.writer(config.db)
        self.addCleanup(store.close)
        with self.assertRaisesRegex(ConfigError, "can't load accounts_provider"):
            build_engine(config, store, executor=None)

    def test_factoryd_uses_the_addons_router(self):
        from lumbridge_factory.factoryd import Config, build_engine
        from lumbridge_factory.store import Store

        d = tempfile.mkdtemp()
        config = Config(
            db=os.path.join(d, "f.sqlite3"), socket="s", gitea_url="http://example.invalid", token_file="t", review_token_file="r",
            accounts=("a", "b"), accounts_provider=f"{self.module}:make", accounts_options={"used": {"a": 80.0}},
        )
        store = Store.writer(config.db)
        self.addCleanup(store.close)
        engine = build_engine(config, store, executor=None)
        self.assertEqual(engine.accounts.pick(Role.BUILDER), "b")
        self.assertEqual(engine.accounts.used_pct("a"), 80.0)


class TightSlotsTest(unittest.TestCase):
    """Opt-in: 3 slots, back to 2 once claude-b passes 70 % of its allowance, or when nobody knows."""

    def engine(self, used):
        from lumbridge_factory.core.policy import default_policy
        from lumbridge_factory.engine import Engine
        from lumbridge_factory.fakes import FakeAccounts
        from lumbridge_factory.store import Store

        d = tempfile.mkdtemp()
        accounts = FakeAccounts("claude-b", "claude-a")
        accounts.used.update(used)
        e = Engine(Store.writer(os.path.join(d, "f.sqlite3")), accounts=accounts)
        self.policy = replace(default_policy(), slots=3, tight_account="claude-b", tight_pct=70.0, tight_slots=2)
        return e

    def test_three_while_the_account_has_room(self):
        self.assertEqual(self.engine({"claude-b": 20.0}).effective_slots(self.policy), 3)

    def test_two_once_it_passes_the_line(self):
        self.assertEqual(self.engine({"claude-b": 70.0}).effective_slots(self.policy), 2)
        self.assertEqual(self.engine({"claude-b": 95.0}).effective_slots(self.policy), 2)

    def test_not_knowing_counts_as_tight(self):
        self.assertEqual(self.engine({}).effective_slots(self.policy), 2)

    def test_a_router_that_reports_nothing_counts_as_tight(self):
        from lumbridge_factory.core.policy import default_policy
        from lumbridge_factory.engine import Engine
        from lumbridge_factory.store import Store

        e = Engine(Store.writer(os.path.join(tempfile.mkdtemp(), "f.sqlite3")), accounts=StaticAccounts(["claude-b"]))
        policy = replace(default_policy(), slots=3, tight_account="claude-b", tight_pct=70.0, tight_slots=2)
        self.assertEqual(e.effective_slots(policy), 2)

    def test_no_tight_account_means_just_slots(self):
        e = self.engine({"claude-b": 99.0})
        self.assertEqual(e.effective_slots(replace(self.policy, tight_account="")), 3)

    def test_the_policy_refuses_nonsense(self):
        from lumbridge_factory.core.policy import PolicyError

        self.engine({})
        with self.assertRaises(PolicyError):
            replace(self.policy, tight_slots=4).validate()
        with self.assertRaises(PolicyError):
            replace(self.policy, tight_pct=0).validate()


if __name__ == "__main__":
    unittest.main()
