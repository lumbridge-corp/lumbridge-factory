"""Fake Sources, a fake forge, a fake executor and fake accounts, for tests and `lumbridge-factory demo`.

The fake forge plays Gitea for both Gates and Channel. The fake executor follows the idempotency
rules of docs/PROTOCOL.md, so recovery is tested against the same contract agentd must honour.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Sequence

from .core.executor import (
    EntryKind,
    EventType,
    ExecutorError,
    InboxEntry,
    Report,
    ReportStatus,
    Role,
    RunningTurn,
    StartRequest,
    TurnEvent,
    TurnOutcome,
    TurnRequest,
    TurnResult,
    Verdict,
    to_wire,
)
from .core.facts import CiFacts, CiFailure, CiState, MainFacts, PrFacts, PrState, ReviewFacts, ReviewState
from .core.model import Decision, RouterDecision, Trigger
from .ports import Assessment, CannotCheck
from .turns import HOLD


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _sha(*parts: Any) -> str:
    return hashlib.sha1(":".join(str(p) for p in parts).encode()).hexdigest()


@dataclass
class FakePr:
    number: int
    repo: str
    head_branch: str
    head_sha: str
    base: str = "main"
    state: PrState = PrState.OPEN
    contains_main_sha: str = ""
    change: str = "change-0"  # what its diff against main says; a main merge keeps it, a real commit changes it


@dataclass
class FakeReview:
    sha: str
    verdict: str
    author: str
    findings: str
    key: str


class FakeForge:
    """Enough of Gitea: PRs, statuses by SHA, reviews by SHA, comments with keys, main."""

    def __init__(self, reviewer_bot: str = "scratch-review"):
        self.reviewer_bot = reviewer_bot
        self.prs: dict[tuple[str, int], FakePr] = {}
        self.ci_by_sha: dict[str, CiFacts] = {}
        self.reviews: dict[tuple[str, int], list[FakeReview]] = {}
        self.comments: list[dict[str, str]] = []
        self.main: dict[str, str] = {}
        self.conflict_on_update = False
        self.updates: list[tuple[str, int]] = []
        self.fail_next_post = 0
        self.cannot_check = False
        self.crash_after_post: BaseException | None = None
        self.down = False
        self.pushed_branches: set[str] = set()
        self.killed: dict[str, list[tuple[int, int]]] = {}
        self.reruns: list[int] = []
        self._next_pr = 100
        self._pushes = 0

    # World changes a test makes.
    def main_sha(self, repo: str) -> str:
        return self.main.setdefault(repo, _sha("main", repo, 0))

    def move_main(self, repo: str) -> str:
        self.main[repo] = _sha("main", repo, self.main_sha(repo))
        return self.main[repo]

    def open_pr(self, repo: str, branch: str, *, ci: CiState = CiState.PENDING, base: str = "main") -> FakePr:
        self._next_pr += 1
        pr = FakePr(self._next_pr, repo, branch, _sha("head", repo, branch, 0), base, contains_main_sha=self.main_sha(repo))
        self.prs[(repo, pr.number)] = pr
        self.set_ci(pr.head_sha, ci)
        return pr

    def push(self, pr: FakePr, *, ci: CiState = CiState.PENDING, same_change: bool = False) -> str:
        self._pushes += 1
        if not same_change:
            pr.change = f"change-{self._pushes}"
        pr.head_sha = _sha("head", pr.repo, pr.head_branch, pr.head_sha, self._pushes)
        pr.contains_main_sha = self.main_sha(pr.repo)
        self.set_ci(pr.head_sha, ci)
        return pr.head_sha

    def set_ci(self, sha: str, state: CiState, log: str = "") -> None:
        failures = (CiFailure("CI / checks (pull_request)", log),) if state == CiState.RED else ()
        self.ci_by_sha[sha] = CiFacts(sha, state, failures)

    def green(self, pr: FakePr) -> None:
        self.set_ci(pr.head_sha, CiState.GREEN)

    def red(self, pr: FakePr, log: str = "boom") -> None:
        self.set_ci(pr.head_sha, CiState.RED, log)

    def merge(self, pr: FakePr) -> None:
        pr.state = PrState.MERGED
        self.move_main(pr.repo)

    def close(self, pr: FakePr) -> None:
        pr.state = PrState.CLOSED

    def add_review(self, repo: str, pr: int, sha: str, verdict: str, author: str, findings: str = "") -> None:
        self.reviews.setdefault((repo, pr), []).append(FakeReview(sha, verdict, author, findings, ""))

    def comments_on(self, thread: str) -> list[str]:
        return [c["text"] for c in self.comments if c["thread"] == thread]

    # Gates.
    def _check_up(self) -> None:
        if self.down:
            raise ConnectionError("forge down")

    def pr(self, repo: str, number: int) -> PrFacts | None:
        self._check_up()
        pr = self.prs.get((repo, number))
        return None if pr is None else PrFacts(pr.number, pr.state, pr.head_sha, pr.head_branch, pr.base)

    def ci(self, repo: str, sha: str, required: tuple[str, ...], runner: str) -> CiFacts:
        self._check_up()
        return self.ci_by_sha.get(sha, CiFacts(sha, CiState.PENDING))

    def review(self, repo: str, number: int, sha: str) -> ReviewFacts:
        self._check_up()
        reviews = self.reviews.get((repo, number), [])
        for i, r in reversed(list(enumerate(reviews))):
            if r.sha == sha:
                return ReviewFacts(sha, ReviewState(r.verdict), r.author, r.findings, i)
        return ReviewFacts(sha, ReviewState.NONE)

    def change(self, repo: str, number: int, sha: str) -> str | None:
        self._check_up()
        pr = self.prs.get((repo, number))
        return None if pr is None or pr.head_sha != sha else f"fp:{pr.change}"

    def current_main(self, repo: str, base: str, sha: str) -> MainFacts:
        self._check_up()
        main = self.main_sha(repo)
        pr = next((p for p in self.prs.values() if p.repo == repo and p.head_sha == sha), None)
        return MainFacts(main, pr is not None and pr.contains_main_sha == main)

    # Channel.
    def find_comment(self, thread: str, key: str) -> bool:
        if self.cannot_check:
            raise CannotCheck("no way to look")
        return any(c["thread"] == thread and c["key"] == key for c in self.comments)

    def post_comment(self, thread: str, text: str, key: str) -> None:
        if self.fail_next_post > 0:
            self.fail_next_post -= 1
            raise ConnectionError("HTTP 503")
        self.comments.append({"thread": thread, "text": text, "key": key})
        if self.crash_after_post is not None:
            crash, self.crash_after_post = self.crash_after_post, None
            raise crash

    def find_review(self, repo: str, pr: int, key: str) -> bool:
        if self.cannot_check:
            raise CannotCheck("no way to look")
        return any(r.key == key for r in self.reviews.get((repo, pr), []))

    def post_review(self, repo: str, pr: int, sha: str, verdict: str, findings: str, key: str) -> None:
        self.reviews.setdefault((repo, pr), []).append(FakeReview(sha, verdict, self.reviewer_bot, findings, key))

    def ensure_pr(self, repo: str, branch: str, base: str, title: str, body: str, key: str) -> dict:
        self._check_up()
        for pr in self.prs.values():
            if pr.repo == repo and pr.head_branch == branch and pr.base == base and pr.state == PrState.OPEN:
                return {"pr": pr.number}
        if branch not in self.pushed_branches:
            return {"refused": f"{branch} isn't on origin"}
        return {"pr": self.open_pr(repo, branch, base=base, ci=CiState.PENDING).number}

    def rerun_job(self, repo: str, run_id: int, job_id: int) -> None:
        self._check_up()
        self.reruns.append(job_id)
        self.killed.clear()

    def killed_jobs(self, repo: str, sha: str, boot_time: float) -> list[tuple[int, int]]:
        self._check_up()
        return list(self.killed.get(sha, ()))

    def update_branch(self, repo: str, pr: int) -> str:
        self.updates.append((repo, pr))
        fake = self.prs[(repo, pr)]
        if self.conflict_on_update:
            return "conflict"
        self.push(fake, same_change=True)  # a clean merge of main: the PR's own change is what it was
        return "updated"


# --- the fake executor -------------------------------------------------------------


def report(status: ReportStatus | str, **kw: Any) -> Report:
    return Report(status=ReportStatus(status), **kw)


def ready(pr: int | None = None, summary: str = "done") -> Report:
    return report(ReportStatus.READY, pr=pr, summary=summary)


def passed() -> Report:
    return report(ReportStatus.READY, verdict=Verdict.PASS, summary="looks right")


def changes(findings: str = "needs a test") -> Report:
    return report(ReportStatus.READY, verdict=Verdict.CHANGES_REQUESTED, findings=findings)


@dataclass
class FakeTurn:
    """One scripted turn. `do` runs when the turn starts (push code, open a PR) and may return
    what to report. `hold` keeps the turn running until released."""

    reports: Sequence[Report] = ()
    replies: Sequence[str] = ()
    follows: Sequence[str] = ()
    handbacks: Sequence[tuple[str, str]] = ()
    recalls: Sequence[str] = ()
    garbage: Sequence[dict] = ()
    outcome: TurnOutcome = TurnOutcome.COMPLETED
    tokens: int = 100
    wall_seconds: float = 10.0
    error: str = ""
    hold: bool = False
    do: Callable[[TurnRequest], Any] | None = None


@dataclass
class _Live:
    request: TurnRequest
    script: FakeTurn
    entries: list[InboxEntry] = field(default_factory=list)
    garbage: list[dict] = field(default_factory=list)
    recalls: list[str] = field(default_factory=list)
    released: bool = False
    result: TurnResult | None = None


class FakeExecutor:
    """Scripts per Assignment id. Without a script a builder turn ends with no report, and a
    reviewer passes."""

    def __init__(self) -> None:
        self.scripts: dict[str, list[FakeTurn | Callable[[TurnRequest], FakeTurn]]] = {}
        self.starts: list[StartRequest] = []
        self.requests: list[TurnRequest] = []
        self.stopped: list[str] = []
        self.forgotten: list[str] = []
        self.removed: list[tuple[str, bool]] = []
        self.recall_answers: list[tuple[str, str, str]] = []
        self.live: dict[str, _Live] = {}
        self.sessions: dict[str, str] = {}
        self.down = False
        self.break_streams = 0  # this many streams die with a dropped socket

    def script(self, assignment: str, *turns: FakeTurn | Callable[[TurnRequest], FakeTurn]) -> None:
        self.scripts.setdefault(assignment, []).extend(turns)

    def requests_for(self, assignment: str) -> list[TurnRequest]:
        return [r for r in self.requests if r.session_id == self.sessions.get(assignment)]

    def text_for(self, assignment: str) -> list[str]:
        return ["\n\n".join(i.text for i in r.inputs) for r in self.requests_for(assignment)]

    def _check_up(self) -> None:
        if self.down:
            raise ExecutorError("unavailable", "agentd is down")

    def start(self, request: StartRequest) -> str:
        self._check_up()
        if request.assignment not in self.sessions:
            self.starts.append(request)
            self.sessions[request.assignment] = f"sess-{request.assignment}"
        return self.sessions[request.assignment]

    def _next_script(self, request: TurnRequest) -> FakeTurn:
        assignment = request.session_id.removeprefix("sess-")
        queue = self.scripts.get(assignment)
        if queue:
            item = queue.pop(0)
            return item(request) if callable(item) else item
        if assignment.rsplit("-", 1)[-1].startswith("r"):
            return FakeTurn(reports=(passed(),))
        return FakeTurn()

    def _begin(self, request: TurnRequest) -> _Live:
        self.requests.append(request)
        script = self._next_script(request)
        if script.do is not None:
            script.do(request)
        live = _Live(request, script)
        seq = 0
        for rep in script.reports:
            seq += 1
            live.entries.append(InboxEntry(seq, EntryKind.REPORT, report=rep))
        for text in script.replies:
            seq += 1
            live.entries.append(InboxEntry(seq, EntryKind.REPLY, purpose="answer", text=text))
        for thread in script.follows:
            seq += 1
            live.entries.append(InboxEntry(seq, EntryKind.FOLLOW, thread=thread))
        for trigger, reason in script.handbacks:
            seq += 1
            live.entries.append(InboxEntry(seq, EntryKind.HANDBACK, trigger=trigger, text=reason))
        live.garbage = list(script.garbage)
        live.recalls = list(script.recalls)
        live.released = not script.hold
        self.live[request.turn_id] = live
        return live

    def turn(self, request: TurnRequest) -> Iterator[Any]:
        self._check_up()
        live = self.live.get(request.turn_id) or self._begin(request)
        return self._stream(live)

    def _stream(self, live: _Live) -> Iterator[Any]:
        request = live.request
        seq = 0
        yield TurnEvent(request.turn_id, (seq := seq + 1), EventType.STARTED, {"native_session_id": f"native-{request.session_id}"})
        for data in live.garbage:
            yield TurnEvent(request.turn_id, (seq := seq + 1), EventType.INBOX, data)
        for i, object_id in enumerate(live.recalls):
            yield TurnEvent(request.turn_id, (seq := seq + 1), EventType.RECALL_REQUEST, {"request_id": f"rq{i}", "object_id": object_id})
        for entry in live.entries:
            yield TurnEvent(request.turn_id, (seq := seq + 1), EventType.INBOX, to_wire(entry))
        if self.break_streams > 0:
            self.break_streams -= 1
            raise ExecutorError("unavailable", "socket closed")
        while not live.released:
            yield HOLD
        if live.result is None:
            live.result = self._finish(live)
        yield live.result

    def _finish(self, live: _Live, outcome: TurnOutcome | None = None) -> TurnResult:
        script = live.script
        return TurnResult(
            turn_id=live.request.turn_id,
            session_id=live.request.session_id,
            outcome=outcome or script.outcome,
            native_session_id=f"native-{live.request.session_id}",
            tokens=script.tokens,
            wall_seconds=script.wall_seconds,
            entries=tuple(live.entries),
            error=script.error,
        )

    def release(self, turn_id: str) -> None:
        self.live[turn_id].released = True

    def held(self) -> list[str]:
        return [t for t, l in self.live.items() if not l.released]

    def stop(self, session_id: str) -> None:
        self._check_up()
        self.stopped.append(session_id)
        for live in self.live.values():
            if live.request.session_id == session_id and not live.released:
                live.result = self._finish(live, TurnOutcome.STOPPED)
                live.released = True

    def running(self) -> list[RunningTurn]:
        self._check_up()
        return [RunningTurn(t, l.request.session_id) for t, l in self.live.items() if not l.released]

    def collect(self, turn_id: str) -> TurnResult | None:
        self._check_up()
        live = self.live.get(turn_id)
        if live is None or not live.released:
            return None
        if live.result is None:
            live.result = self._finish(live)
        return live.result

    def forget(self, turn_id: str) -> None:
        self.forgotten.append(turn_id)

    def remove_session(self, session_id: str, worktree: bool) -> None:
        self._check_up()
        if any(l.request.session_id == session_id and not l.released for l in self.live.values()):
            raise ExecutorError("unavailable", "this session has a running turn")
        self.removed.append((session_id, worktree))

    def answer_recall(self, session_id: str, request_id: str, text: str) -> None:
        self.recall_answers.append((session_id, request_id, text))

    # What a restart does to turns (PROTOCOL.md, Turn idempotency).
    def host_restart(self, keep_entries: bool = False) -> None:
        """Every running turn is cut off; agentd's journal replays them as INTERRUPTED."""
        for live in self.live.values():
            if not live.released:
                live.result = self._finish(live, TurnOutcome.INTERRUPTED)
                if not keep_entries:
                    live.result = TurnResult(
                        live.result.turn_id, live.result.session_id, TurnOutcome.INTERRUPTED,
                        live.result.native_session_id, tokens=live.result.tokens, wall_seconds=live.result.wall_seconds,
                    )
                live.released = True

    def lose_journal(self) -> None:
        """agentd came back with nothing at all."""
        self.live.clear()


class FakeSource:
    name = "gitea"

    def __init__(self, name: str = "gitea"):
        self.name = name
        self.events: list[Trigger] = []
        self.polled_from: list[str | None] = []

    def poll(self, cursor: str | None) -> tuple[list[Trigger], str]:
        self.polled_from.append(cursor)
        start = int(cursor or 0)
        return self.events[start:], str(len(self.events))


class FakeAccounts:
    def __init__(self, *names: str):
        self.names = list(names or ("claude-a", "claude-b"))
        self.limited: set[str] = set()
        self.used: dict[str, float | None] = {}

    def used_pct(self, name: str) -> float | None:
        return self.used.get(name)

    def pick(self, role: Role, avoid: str = "") -> str | None:
        usable = [n for n in self.names if n not in self.limited]
        for name in usable:
            if name != avoid:
                return name
        return usable[0] if usable else None

    def mark_limited(self, account: str) -> None:
        self.limited.add(account)


class FakeAssessor:
    def __init__(self, decision: Decision = Decision.IGNORE, **kw: Any):
        self.answer = RouterDecision(decision, **kw)
        self.requests: list[Assessment] = []
        self.down = False

    def assess(self, request: Assessment) -> RouterDecision:
        if self.down:
            raise ConnectionError("router down")
        self.requests.append(request)
        return self.answer

