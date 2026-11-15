"""The acceptance gates (§5): the interface, one read of all three, and the real one on Gitea.

The rule: acceptance comes only from what these return, read by the host. A worker's report is
never an input here. An implementation must:
  - ci: count only the policy's required contexts, on exactly this SHA, from the policy's runner.
  - review: return the newest review on exactly this SHA, with its real author.
  - current_main: answer whether `sha` contains the tip of `base`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from typing import Callable, Protocol

from .channels.gitea import parse_verdict
from .core.facts import CiFacts, CiFailure, CiState, GateFacts, MainFacts, PrFacts, PrState, ReviewFacts, ReviewState
from .core.model import Ticket
from .core.policy import Policy
from .gitea_api import GiteaClient, GiteaError, branch_path, parse_time, repo_path


class Gates(Protocol):
    def pr(self, repo: str, number: int) -> PrFacts | None: ...

    def ci(self, repo: str, sha: str, required: tuple[str, ...], runner: str) -> CiFacts: ...

    def review(self, repo: str, number: int, sha: str) -> ReviewFacts: ...

    def current_main(self, repo: str, base: str, sha: str) -> MainFacts: ...

    def change(self, repo: str, number: int, sha: str) -> str | None:
        """A fingerprint of the PR's diff against its base, if its head is still `sha`; else None."""
        ...


def trusted_runners(runner: str) -> set[str]:
    """`ci_runner` names one runner, or several separated by commas (an instance may run several, started on demand). A green counts only from a runner named here."""
    return {name.strip() for name in runner.split(",") if name.strip()}


def change_fingerprint(diff: str) -> str:
    """The PR's own change, without what merging main moves around: no `index` lines and no hunk line numbers. File names,
    context and the changed lines all count, so a conflict resolved differently, or main changing lines next to the
    PR's, is a different change and gets a fresh review."""
    keep = []
    for line in diff.splitlines():
        if line.startswith("index "):
            continue
        if line.startswith("@@"):
            keep.append("@@")
            continue
        keep.append(line.rstrip())
    return hashlib.sha256("\n".join(keep).encode()).hexdigest()


def read_facts(gates: Gates, t: Ticket, policy: Policy) -> GateFacts | None:
    """One read of the forge, all three gates on the same head. None if there's no PR to read."""
    number = t.pr_claim if t.pr_claim is not None else t.pr
    if number is None:
        return None
    pr = gates.pr(t.repo, number)
    if pr is None or pr.state != PrState.OPEN:
        return GateFacts(number=number, pr=pr)
    repo = policy.repo(t.repo)
    required, runner = (repo.required_checks, repo.ci_runner) if repo else ((), "")
    if repo is not None and repo.carry_review_across_main:
        pr = replace(pr, change=gates.change(t.repo, number, pr.head_sha))
    return GateFacts(
        number=number,
        pr=pr,
        ci=gates.ci(t.repo, pr.head_sha, required, runner),
        review=gates.review(t.repo, number, pr.head_sha),
        main=gates.current_main(t.repo, t.base, pr.head_sha),
    )


# --- the real one ---------------------------------------------------------------------

_GREEN = "success"
_RED = ("failure", "error", "warning")
_FAILED_JOB = ("failure", "cancelled", "timed_out")
_LOG_CHARS = 16000
_FAILURE_LINES = 25
# Optional timestamp, then a word a test runner or a compiler starts a failure with.
_FAILURE_LINE = re.compile(r"^(\S*Z\s+)?(FAIL|FAILED|not ok|error(\[|:)|##\[error\]|test result: FAILED|thread .* panicked)", re.I)
# Gitea marks a job that lost its runner as failed some minutes later, or just before the host is back.
_OUTAGE_MARGIN_SECONDS = 15 * 60.0


def _job_context_name(context: str) -> str:
    """"CI / checks (pull_request)" is job "checks" of workflow "CI", on that event."""
    name = context.split(" / ", 1)[-1]
    return name.rsplit(" (", 1)[0] if name.endswith(")") else name


def failure_excerpt(log: str) -> str:
    """The log's end, then the lines that say what failed, last because a card keeps the tail (§10). A job that runs every suite before
    it fails ends in other suites' passes: on one early PR the builder got 60 lines of them and had to guess which check was red."""
    found = [line for line in log.splitlines() if _FAILURE_LINE.search(line)]
    if not found:
        return log[-_LOG_CHARS:]
    picked = found[-_FAILURE_LINES:]
    return log[-_LOG_CHARS:] + f"\n\nLines in the whole log that look like the failure ({len(picked)} of {len(found)}):\n" + "\n".join(line[:300] for line in picked)


class GiteaGates:
    """Each call is a fresh read of the forge. Nothing here is cached, so the engine spaces reads out
    instead (`gate_read_seconds`) and every decision sees one moment."""

    def __init__(self, client: GiteaClient, policy: Callable[[], Policy]):
        self.client = client
        self.policy = policy

    def pr(self, repo: str, number: int) -> PrFacts | None:
        data = self.client.get_or_none(f"{repo_path(repo)}/pulls/{number}")
        if data is None:
            return None
        head, base = data["head"], data["base"]
        state = PrState.MERGED if data.get("merged") else PrState.OPEN if data["state"] == "open" else PrState.CLOSED
        # A PR from a fork names another repo's branch, so it can't pass as the Ticket's own.
        origin = (head.get("repo") or {}).get("full_name") or repo
        branch = head["ref"] if origin == repo else f"{origin}:{head['ref']}"
        return PrFacts(number, state, head["sha"], branch, base["ref"])

    def ci(self, repo: str, sha: str, required: tuple[str, ...], runner: str) -> CiFacts:
        """Only required contexts, on this SHA, newest status of each. Missing means pending."""
        latest: dict[str, dict] = {}
        for status in self.client.pages(f"{repo_path(repo)}/commits/{sha}/statuses"):
            context = status.get("context")
            if context in required and (context not in latest or status["id"] > latest[context]["id"]):
                latest[context] = status
        failures = tuple(
            CiFailure(context, self._failure_log(repo, sha, status))
            for context, status in latest.items()
            if status.get("status") in _RED
        )
        if failures:
            return CiFacts(sha, CiState.RED, failures)
        if len(latest) < len(required) or any(s.get("status") != _GREEN for s in latest.values()):
            return CiFacts(sha, CiState.PENDING)
        if runner and not all(self._from_runner(repo, sha, context, runner) for context in required):
            return CiFacts(sha, CiState.PENDING)  # green, but we can't say it came from the right runner
        return CiFacts(sha, CiState.GREEN)

    def _jobs(self, repo: str, sha: str) -> list[dict]:
        out: list[dict] = []
        for run in (self.client.get(f"{repo_path(repo)}/actions/runs", head_sha=sha, limit=50) or {}).get("workflow_runs") or []:
            for job in (self.client.get(f"{repo_path(repo)}/actions/runs/{run['id']}/jobs") or {}).get("jobs") or []:
                out.append({**job, "run_id": job.get("run_id") or run["id"]})
        return out

    def _from_runner(self, repo: str, sha: str, context: str, runner: str) -> bool:
        name = _job_context_name(context)
        jobs = [j for j in self._jobs(repo, sha) if j.get("name") == name and j.get("head_sha", sha) == sha]
        return bool(jobs) and jobs[-1].get("conclusion") == _GREEN and jobs[-1].get("runner_name") in trusted_runners(runner)

    def _failure_log(self, repo: str, sha: str, status: dict) -> str:
        text = str(status.get("description") or "")
        name = _job_context_name(status.get("context") or "")
        try:
            for job in self._jobs(repo, sha):
                if job.get("name") == name and job.get("conclusion") in _FAILED_JOB:
                    return failure_excerpt(self.client.text(f"{repo_path(repo)}/actions/jobs/{job['id']}/logs"))
        except GiteaError:
            pass  # the description is still something to go on
        return text or "No log available."

    def killed_jobs(self, repo: str, sha: str, boot_time: float) -> list[tuple[int, int]]:
        """§8: jobs on this SHA that were running when the host went down: started before it came
        back, and either still marked running or failed around then. Each is (run id, job id)."""
        killed = []
        for job in self._jobs(repo, sha):
            started = parse_time(job.get("started_at"))
            if not started or started >= boot_time:
                continue
            running = job.get("status") in ("in_progress", "running", "waiting", "queued")
            died = job.get("conclusion") in _FAILED_JOB and parse_time(job.get("completed_at")) >= boot_time - _OUTAGE_MARGIN_SECONDS
            if running or died:
                killed.append((job["run_id"], job["id"]))
        return killed

    def review(self, repo: str, number: int, sha: str) -> ReviewFacts:
        """The newest verdict on exactly this SHA by a reviewer bot. A human's words, or anyone else's
        footer, are not a verdict. The owner's review comment is a follow-up, read by the Source."""
        bots = self.policy().reviewer_bots
        best = None
        for comment in self.client.pages(f"{repo_path(repo)}/issues/{number}/comments"):
            if (comment.get("user") or {}).get("login") not in bots:
                continue
            verdict = parse_verdict(comment.get("body") or "")
            if verdict is not None and verdict.sha == sha and (best is None or comment["id"] > best[0]):
                best = (comment["id"], comment["user"]["login"], verdict)
        if best is None:
            return ReviewFacts(sha, ReviewState.NONE)
        return ReviewFacts(sha, ReviewState(best[2].verdict), best[1], best[2].findings, best[0])

    def change(self, repo: str, number: int, sha: str) -> str | None:
        """`pulls/<n>.diff` is the diff at whatever the head is when it answers, so the head is read before and after: if it
        moved, nothing is claimed."""
        try:
            before = self.client.get_or_none(f"{repo_path(repo)}/pulls/{number}")
            diff = self.client.text(f"{repo_path(repo)}/pulls/{number}.diff")
            after = self.client.get_or_none(f"{repo_path(repo)}/pulls/{number}")
        except GiteaError:
            return None
        if not before or not after or before["head"]["sha"] != sha or after["head"]["sha"] != sha or not diff.strip():
            return None
        return change_fingerprint(diff)

    def current_main(self, repo: str, base: str, sha: str) -> MainFacts:
        """Measured on 28.2: `compare/<head>...<main>` counts what main has that the head lacks. The
        PR's `merge_base` field goes stale after an update, so it isn't used."""
        main = self.client.get(f"{repo_path(repo)}/branches/{branch_path(base)}")["commit"]["id"]
        ahead = self.client.get(f"{repo_path(repo)}/compare/{sha}...{main}")
        return MainFacts(main, int(ahead.get("total_commits") or 0) == 0)
