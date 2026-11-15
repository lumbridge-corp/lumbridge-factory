"""One fake Ticket in each state a person should recognise, on fakes only."""

from __future__ import annotations

from .core.facts import CiState
from .core.model import Trigger, TriggerKind, issue_thread, ticket_id
from .core.policy import default_policy
from .engine import Engine
from .fakes import FakeAccounts, FakeClock, FakeExecutor, FakeForge, FakeTurn, ready, report
from .store import Store

REPO = "acme/widgets"


def run_demo(db: str) -> None:
    store = Store.writer(db)
    clock, forge, executor = FakeClock(), FakeForge(reviewer_bot="demo-review"), FakeExecutor()
    engine = Engine(store, executor=executor, gates=forge, channel=forge, accounts=FakeAccounts(), clock=clock)
    engine.bootstrap(default_policy("demo", REPO))

    def issue(n: int, title: str) -> Trigger:
        return Trigger(
            "gitea", f"issue-{n}", issue_thread(REPO, n), TriggerKind.ISSUE_OPENED, "owner",
            body=f"Please: {title.lower()}.", repo=REPO, data={"labels": ["factory"], "title": title},
        )

    def opens_pr(tid: str, ci: CiState):
        def turn(_):
            return FakeTurn(reports=(ready(forge.open_pr(REPO, f"factory/{tid}", ci=ci).number),), tokens=42_000, wall_seconds=600)

        return turn

    scripts = {
        1: ("Dark mode toggle", lambda tid: opens_pr(tid, CiState.GREEN)),
        2: ("Fix the flaky test", lambda tid: opens_pr(tid, CiState.PENDING)),
        3: ("Rename the settings page", lambda tid: FakeTurn(reports=(report("blocked", question="Should the old URL keep working?"),), tokens=9_000)),
        4: ("Add usage graphs", lambda tid: FakeTurn(hold=True, tokens=1_000)),
    }
    for n, (title, turn) in scripts.items():
        trigger = issue(n, title)
        tid = ticket_id(trigger)
        executor.script(f"{tid}-b1", turn(tid))
        engine.ingest(trigger)
    engine.settle()
    store.close()
