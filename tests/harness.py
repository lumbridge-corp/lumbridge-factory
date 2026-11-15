"""A scratch world: a real store in a temp dir, fake everything outside."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from typing import Any

from lumbridge_factory.core.executor import TurnRequest
from lumbridge_factory.core.facts import CiState
from lumbridge_factory.core.model import ActorKind, Trigger, TriggerKind, issue_thread, pr_thread, ticket_id
from lumbridge_factory.core.policy import default_policy
from lumbridge_factory.engine import Engine
from lumbridge_factory.fakes import FakeAccounts, FakeAssessor, FakeClock, FakeExecutor, FakeForge, FakePr, FakeTurn, ready
from lumbridge_factory.store import Store

REPO = "acme/sandbox"


class World:
    def __init__(self, directory: str, **policy_changes: Any):
        self.dir = directory
        self.path = os.path.join(directory, "factory.sqlite3")
        self.clock = FakeClock()
        self.forge = FakeForge()
        self.executor = FakeExecutor()
        self.accounts = FakeAccounts()
        self.assessor = FakeAssessor()
        self.sources: list = []
        self._seq = 0
        self.store = Store.writer(self.path)
        self.engine = self._engine()
        self.engine.bootstrap(replace(default_policy(), **policy_changes))

    def _engine(self) -> Engine:
        return Engine(
            self.store,
            executor=self.executor,
            gates=self.forge,
            channel=self.forge,
            accounts=self.accounts,
            assessor=self.assessor,
            sources=self.sources,
            clock=self.clock,
        )

    def restart(self) -> None:
        """factoryd dies and comes back: a new store handle and engine, same outside world."""
        self.store.close()
        self.store = Store.writer(self.path)
        self.engine = self._engine()

    def settle(self) -> None:
        self.engine.settle()

    # Events from outside.
    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    def issue(self, number: int = 1, body: str = "Add a button", *, actor: str = "owner", labels=("factory",), title: str = "Add a button", **data) -> str:
        trigger = Trigger(
            source="gitea",
            external_id=self._next("issue"),
            thread=issue_thread(REPO, number),
            kind=TriggerKind.ISSUE_OPENED,
            actor=actor,
            body=body,
            repo=REPO,
            data={"labels": list(labels), "title": title, **data},
        )
        self.engine.ingest(trigger)
        return ticket_id(trigger)

    def comment(self, thread: str, body: str, *, actor: str = "owner", actor_kind: ActorKind = ActorKind.PERSON, source: str = "gitea", repo: str = REPO) -> Trigger:
        trigger = Trigger(
            source=source,
            external_id=self._next("comment"),
            thread=thread,
            kind=TriggerKind.COMMENT,
            actor=actor,
            actor_kind=actor_kind,
            body=body,
            repo=repo,
        )
        self.engine.ingest(trigger)
        return trigger

    def fact(self, kind: TriggerKind, thread: str, **data) -> Trigger:
        trigger = Trigger(
            source="gitea",
            external_id=self._next("fact"),
            thread=thread,
            kind=kind,
            actor="gitea",
            actor_kind=ActorKind.SYSTEM,
            repo=REPO,
            data=data,
        )
        self.engine.ingest(trigger)
        return trigger

    # Reading.
    def ticket(self, id: str):
        return self.store.ticket(id)

    def comments(self, number: int = 1) -> list[str]:
        return self.forge.comments_on(issue_thread(REPO, number))

    def builder_texts(self, id: str, n: int = 1) -> list[str]:
        return self.executor.text_for(f"{id}-b{n}")

    # Common builder behaviour.
    def opens_pr(self, id: str, *, ci: CiState = CiState.PENDING, base: str = "main") -> dict:
        """A script item: the builder pushes, opens a PR and reports ready."""
        holder: dict = {}

        def turn(_: TurnRequest) -> FakeTurn:
            pr = self.forge.open_pr(REPO, f"factory/{id}", ci=ci, base=base)
            holder["pr"] = pr
            return FakeTurn(reports=(ready(pr.number),))

        holder["turn"] = turn
        return holder

    def to_gating(self, number: int = 1, *, ci: CiState = CiState.PENDING) -> tuple[str, FakePr]:
        """Issue -> builder -> PR, settled, waiting on the gates."""
        id = ticket_id(
            Trigger("gitea", f"issue-{self._seq + 1}", issue_thread(REPO, number), TriggerKind.ISSUE_OPENED, "owner")
        )
        step = self.opens_pr(id, ci=ci)
        self.executor.script(f"{id}-b1", step["turn"])
        assert self.issue(number) == id
        self.settle()
        return id, step["pr"]

    def fixes(self, pr: FakePr, *, ci: CiState = CiState.PENDING, **kw) -> Any:
        """A script item: the builder pushes a new commit and reports ready."""

        def turn(_: TurnRequest) -> FakeTurn:
            self.forge.push(pr, ci=ci)
            return FakeTurn(reports=(ready(pr.number),), **kw)

        return turn

    def close(self) -> None:
        self.store.close()


class WorldTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.mkdtemp(prefix="lumbridge-factory-test-")
        self.addCleanup(shutil.rmtree, self._dir, True)

    def world(self, **policy_changes: Any) -> World:
        world = World(tempfile.mkdtemp(dir=self._dir), **policy_changes)
        self.addCleanup(world.close)
        return world


__all__ = ["World", "WorldTest", "REPO", "pr_thread", "issue_thread"]
