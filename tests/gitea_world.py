"""The engine wired as factoryd wires it, on a fake Gitea over HTTP, with a scripted executor."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from typing import Any

from fake_gitea import REPO, FakeGitea

from lumbridge_factory.core.policy import RepoPolicy, default_policy
from lumbridge_factory.factoryd import Config, build_engine
from lumbridge_factory.fakes import FakeClock, FakeExecutor, FakeTurn, ready
from lumbridge_factory.store import Store
from lumbridge_factory.turns import InlineRunner

logging.getLogger("lumbridge_factory").setLevel(logging.CRITICAL)  # outages here are on purpose

CHECK = "CI / checks (pull_request)"
POLICY = replace(
    default_policy("sandbox", REPO),
    repositories=(RepoPolicy(REPO, required_checks=(CHECK,)),),
    bots=("factory-bot",),
    reviewer_bots=("review-bot",),
    builder_model="sonnet",
    reviewer_model="sonnet",
)


class PushingExecutor(FakeExecutor):
    """With no script, a builder pushes its branch and reports ready, and stays quiet otherwise."""

    def __init__(self, world: "GiteaWorld"):
        super().__init__()
        self.world = world
        self.next_builder: list = []  # for a Ticket whose id a test can't know yet

    def _next_script(self, request):
        assignment = request.session_id.removeprefix("sess-")
        if not self.scripts.get(assignment) and "-b" in assignment:
            item = self.next_builder.pop(0) if self.next_builder else self.world.pushes()
            return item(request) if callable(item) else item
        return super()._next_script(request)


class GiteaWorld:
    def __init__(self, directory: str, policy=POLICY, factory_token="factory-token", review_token="review-token", **config: Any):
        self.dir = directory
        self.fake = FakeGitea()
        self.executor = PushingExecutor(self)
        self.clock = None
        self.policy = policy
        for name in ("factory-token", "review-token", "owner-token"):
            with open(os.path.join(directory, name), "w") as fh:
                fh.write(name)
        self.config = Config(
            db=os.path.join(directory, "factory.sqlite3"),
            socket=os.path.join(directory, "agentd.sock"),
            gitea_url=self.fake.url,
            token_file=os.path.join(directory, factory_token),
            review_token_file=os.path.join(directory, review_token),
            accounts=("claude-a", "claude-b"),
            policy=policy.to_dict(),
            tick_seconds=0.01,
            **{"poll_seconds": 0.0, "gate_read_seconds": 0.0, "retain_ended_seconds": 1000.0, **config},
        )
        self.clock_obj = FakeClock(1_900_000_000.0)
        self.store = None
        self.engine = None
        self.start()

    def start(self) -> None:
        """A new process's worth of state: new handle, new engine, same world."""
        if self.store is not None:
            self.store.close()
        self.store = Store.writer(self.config.db)
        self.engine = build_engine(self.config, self.store, self.executor, clock=self.clock_obj)
        self.engine.runner = InlineRunner()
        self.engine.boot_time = None
        self.engine.bootstrap(self.policy)

    def settle(self) -> None:
        self.engine.settle()

    def close(self) -> None:
        self.store.close()
        self.fake.close()

    # --- reading ---------------------------------------------------------------------

    def tickets(self):
        return self.store.tickets()

    def only_ticket(self):
        tickets = self.tickets()
        assert len(tickets) == 1, tickets
        return tickets[0]

    def routing(self, external_id: str) -> str:
        row = self.store.conn.execute(
            "SELECT r.status FROM triggers t JOIN trigger_routing r ON r.trigger_id = t.id WHERE t.external_id = ?", (external_id,)
        ).fetchone()
        return row["status"] if row else ""

    def trigger_count(self) -> int:
        return self.store.conn.execute("SELECT COUNT(*) FROM triggers WHERE source = 'gitea' AND kind NOT IN ('ci_status', 'main_moved')").fetchone()[0]

    def builder_text(self, ticket_id: str, n: int = 1) -> str:
        return "\n\n".join(self.executor.text_for(f"{ticket_id}-b{n}"))

    # --- a builder ----------------------------------------------------------------------

    def pushes(self, branch: str | None = None, **turn: Any):
        """A script item: the builder pushes its branch and reports ready, with no PR number."""

        def run(request) -> FakeTurn:
            ticket_id = request.session_id.removeprefix("sess-").rsplit("-", 1)[0]
            self.fake.push(branch or f"factory/{ticket_id}")
            return FakeTurn(reports=(ready(None, "Did the work."),), **turn)

        return run


class GiteaWorldTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.mkdtemp(prefix="lumbridge-factory-gw-")
        self.addCleanup(shutil.rmtree, self._dir, True)

    def world(self, **kw: Any) -> GiteaWorld:
        world = GiteaWorld(tempfile.mkdtemp(dir=self._dir), **kw)
        self.addCleanup(world.close)
        return world


def pretty(store: Store) -> str:
    return json.dumps([dict(r) for r in store.conn.execute("SELECT t.external_id, r.status, r.reason FROM triggers t JOIN trigger_routing r ON r.trigger_id = t.id")], indent=1)
