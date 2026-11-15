"""The write side of a Gitea Thread (§4), and the footer that makes every write checkable (§8).

Every comment ends in a hidden footer with a hash of its Outbox key. A retry after a crash looks for
that footer before writing. A reviewer's verdict is a comment too, by the reviewer account, whose
footer also names the exact SHA and the verdict: gate 2 reads only those, only from that account.

Nothing a worker or an outsider wrote can carry a footer, because the Channel disarms any it finds
in the text before adding its own. With a shared sandbox account that's all that stands between a
builder's `reply` and a forged pass.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable

from ..names import TAG
from ..gitea_api import GiteaClient, GiteaError, branch_path, repo_path
from ..ports import CannotCheck

_FOOTER = re.compile(
    r"\s*<!-- " + re.escape(TAG) + r":key=(?P<key>[0-9a-f]{32})(?: review sha=(?P<sha>[0-9a-f]{40,64}) verdict=(?P<verdict>pass|changes_requested))? -->\s*\Z"
)
_ANY_FOOTER = re.compile(r"<!--\s*" + re.escape(TAG), re.I)
_THREAD = re.compile(r"^(?P<repo>[^#]+)#(?P<kind>issue|pr)/(?P<number>\d+)(?:/ci)?$")


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def disarm(text: str) -> str:
    """Outside text can look like anything except our footer."""
    return _ANY_FOOTER.sub("&lt;!-- " + TAG, text)


def with_footer(text: str, key: str, *, sha: str = "", verdict: str = "") -> str:
    review = f" review sha={sha} verdict={verdict}" if sha else ""
    return f"{disarm(text).rstrip()}\n\n<!-- {TAG}:key={key_hash(key)}{review} -->"


def has_footer(body: str) -> bool:
    return _FOOTER.search(body or "") is not None


def body_key_hash(body: str) -> str | None:
    match = _FOOTER.search(body or "")
    return match["key"] if match else None


@dataclass(frozen=True)
class ParsedVerdict:
    sha: str
    verdict: str
    findings: str


def parse_verdict(body: str) -> ParsedVerdict | None:
    match = _FOOTER.search(body or "")
    if match is None or not match["sha"]:
        return None
    return ParsedVerdict(match["sha"], match["verdict"], body[: match.start()].strip())


def thread_target(thread: str) -> tuple[str, int] | None:
    """(repo, number) of an issue or PR Thread. None for one with no forge object, like main's CI."""
    match = _THREAD.match(thread)
    return (match["repo"], int(match["number"])) if match else None


class GiteaChannel:
    """`client` writes as the factory's account, `review_client` as the reviewer's. They're the same
    account only on the sandbox."""

    def __init__(self, client: GiteaClient, review_client: GiteaClient | None = None):
        self.client = client
        self.review_client = review_client or client
        self._logins: set[str] | None = None

    def own_logins(self) -> set[str]:
        if self._logins is None:
            self._logins = {self.client.login(), self.review_client.login()}
        return self._logins

    def _comments(self, repo: str, number: int) -> Iterable[dict]:
        return self.client.pages(f"{repo_path(repo)}/issues/{number}/comments")

    def _find(self, repo: str, number: int, key: str) -> bool:
        wanted, own = key_hash(key), self.own_logins()
        # Only our own accounts' footers count, or an outsider could silence a comment in advance.
        return any(body_key_hash(c.get("body")) == wanted and (c.get("user") or {}).get("login") in own for c in self._comments(repo, number))

    def find_comment(self, thread: str, key: str) -> bool:
        target = thread_target(thread)
        if target is None:
            raise CannotCheck(f"{thread} is not an issue or a PR")
        return self._find(*target, key)

    def post_comment(self, thread: str, text: str, key: str) -> None:
        target = thread_target(thread)
        if target is None:
            raise CannotCheck(f"{thread} is not an issue or a PR")
        repo, number = target
        self.client.post(f"{repo_path(repo)}/issues/{number}/comments", {"body": with_footer(text, key)})

    def find_review(self, repo: str, pr: int, key: str) -> bool:
        return self._find(repo, pr, key)

    def post_review(self, repo: str, pr: int, sha: str, verdict: str, findings: str, key: str) -> None:
        title = "passed" if verdict == "pass" else "asks for changes"
        text = f"**Independent review of `{sha[:10]}` {title}.**"
        if findings.strip():
            text += "\n\n" + findings.strip()
        self.review_client.post(f"{repo_path(repo)}/issues/{pr}/comments", {"body": with_footer(text, key, sha=sha, verdict=verdict)})

    def update_branch(self, repo: str, pr: int) -> str:
        path = f"{repo_path(repo)}/pulls/{pr}/update"
        try:
            self.client.post(path, style="merge")
        except GiteaError as exc:
            if exc.status == 409:
                return "conflict"
            # Measured on 28.2: a repeat answers 500 "HeadBranch of PR 3 is up to date".
            if exc.status == 500 and "up to date" in exc.message:
                return "updated"
            raise
        return "updated"

    def ensure_pr(self, repo: str, branch: str, base: str, title: str, body: str, key: str) -> dict:
        existing = self._open_pr(repo, branch, base)
        if existing is not None:
            return {"pr": existing}
        try:
            made = self.client.post(f"{repo_path(repo)}/pulls", {"head": branch, "base": base, "title": title, "body": with_footer(body, key)})
        except GiteaError as exc:
            if exc.status == 409:  # measured: someone just opened it
                existing = self._open_pr(repo, branch, base)
                if existing is not None:
                    return {"pr": existing}
            if exc.status in (404, 409, 422):
                return {"refused": exc.message}  # no such branch, or nothing to merge
            raise
        return {"pr": made["number"]}

    def _open_pr(self, repo: str, branch: str, base: str) -> int | None:
        for pr in self.client.pages(f"{repo_path(repo)}/pulls", state="open"):
            head, target = pr.get("head") or {}, pr.get("base") or {}
            same_repo = ((head.get("repo") or {}).get("full_name") or repo) == repo
            if same_repo and head.get("ref") == branch and target.get("ref") == base:
                return pr["number"]
        return None

    def rerun_job(self, repo: str, run_id: int, job_id: int) -> None:
        self.client.post(f"{repo_path(repo)}/actions/runs/{run_id}/jobs/{job_id}/rerun")


__all__ = ["GiteaChannel", "branch_path", "has_footer", "parse_verdict", "with_footer", "disarm", "key_hash", "thread_target"]
