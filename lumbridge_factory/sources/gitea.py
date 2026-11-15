"""The read side of Gitea (§4): polling, not webhooks, because the forge may not be able to reach the host.

Each poll turns what changed into Triggers, and the cursor moves in the same transaction that stores
them, so a restart neither loses nor replays an event. Triggers are stored once by their external id,
so overlapping reads (timestamps are whole seconds, and `since` is inclusive) are harmless.

Who wrote something comes only from the forge's own fields. Who gets to act on it is the gate's call
(core/intake.py): a stranger's comment is recorded here and goes no further. The factory's own writes
are dropped here, by login, or on a shared sandbox account by the footer the Channel writes.

The first poll takes a baseline: what was already there isn't work. Comments are stored once, so an
edit changes nothing.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from ..channels.gitea import has_footer
from ..core.facts import CiState
from ..core.intake import is_factory_command
from ..core.model import ActorKind, Trigger, TriggerKind, ci_thread, issue_thread, main_ci_thread, pr_thread
from ..core.policy import Policy, RepoPolicy
from ..gitea_api import GiteaClient, GiteaError, branch_path, format_time, parse_time, repo_path

log = logging.getLogger("lumbridge_factory.sources.gitea")

FACTORY_LABEL = "factory"
_CURSOR_VERSION = 1
# A change in the same second as the last read has the same `updated_at`. Read a PR again this long.
_SAME_SECOND_SECONDS = 30.0
_COMMENT_TYPE_LABEL = "label"
_CI_FACT = {CiState.GREEN: "green", CiState.RED: "red", CiState.PENDING: "pending"}


def _login(obj: dict | None) -> str:
    return str((obj or {}).get("login") or "")


def _labels(issue: dict) -> list[str]:
    return [str(l.get("name")) for l in issue.get("labels") or []]


class GiteaSource:
    name = "gitea"

    def __init__(self, client: GiteaClient, policy: Callable[[], Policy], ci: Callable[[str, str, tuple[str, ...], str], Any]):
        self.client = client
        self.policy = policy
        self.ci = ci  # the gates' CI read: one definition of green, for facts and for acceptance

    # --- the poll ------------------------------------------------------------------

    def poll(self, cursor: str | None) -> tuple[list[Trigger], str]:
        state = json.loads(cursor) if cursor else {"v": _CURSOR_VERSION, "repos": {}}
        events: list[Trigger] = []
        for repo in self.policy().repositories:
            baseline = repo.name not in state["repos"]
            repo_state = state["repos"].setdefault(repo.name, {})
            if baseline:
                self._baseline(repo, repo_state)
            events += self._issues(repo, repo_state)
            events += self._comments(repo, repo_state)
            events += self._pulls(repo, repo_state)
            events += self._main(repo, repo_state)
        return events, json.dumps(state, sort_keys=True)

    def _now(self) -> float:
        return self.client.server_time or 0.0

    def _baseline(self, repo: RepoPolicy, state: dict) -> None:
        """Everything already there stays out. The owner's next label or comment is what counts."""
        path = repo_path(repo.name)
        self.client.get(path)  # sets server_time; refuses a poll that can't reach the forge
        since = format_time(self._now())
        state.update(baseline=since, issues_since=since, comments_since=since, labelled={}, prs={})
        for issue in self.client.pages(f"{path}/issues", state="open", type="issues", labels=FACTORY_LABEL):
            state["labelled"][str(issue["number"])] = issue["updated_at"]
        for pr in self.client.pages(f"{path}/pulls", state="open"):
            state["prs"][str(pr["number"])] = pr["updated_at"]
            for review in self._reviews(repo.name, pr["number"]):
                state.setdefault("reviews_seen", []).append(review["id"])

    # --- issues ----------------------------------------------------------------------

    def _issues(self, repo: RepoPolicy, state: dict) -> list[Trigger]:
        path, events = repo_path(repo.name), []
        # New issues, whose only part here is triage: an unlabeled issue by the owner is observed.
        since = state["issues_since"]
        for issue in self.client.pages(f"{path}/issues", state="all", type="issues", since=since):
            state["issues_since"] = max(state["issues_since"], _utc(issue["updated_at"]))
            # The label is not read off the issue: who put it there decides, below.
            if parse_time(issue["created_at"]) >= parse_time(since) and FACTORY_LABEL not in _labels(issue):
                events.append(self._opened(repo.name, issue))
        # The label: who applied it, from the forge's timeline, never who wrote the issue.
        labelled = state["labelled"]
        current = {}
        for issue in self.client.pages(f"{path}/issues", state="open", type="issues", labels=FACTORY_LABEL):
            number = str(issue["number"])
            current[number] = issue["updated_at"]
            if labelled.get(number) != issue["updated_at"]:
                events += self._labelled(repo.name, issue, state["baseline"])
        state["labelled"] = current
        return events

    def _opened(self, repo: str, issue: dict) -> Trigger:
        return Trigger(
            source=self.name,
            external_id=f"{repo}/issue/{issue['number']}/opened",
            thread=issue_thread(repo, issue["number"]),
            kind=TriggerKind.ISSUE_OPENED,
            actor=_login(issue.get("user")),
            body=issue.get("body") or "",
            repo=repo,
            at=parse_time(issue["created_at"]),
            data={"labels": [], "title": issue.get("title") or "", "issue_author": _login(issue.get("user")), "number": issue["number"]},
        )

    def _labelled(self, repo: str, issue: dict, baseline: str) -> list[Trigger]:
        number, events = issue["number"], []
        for item in self.client.pages(f"{repo_path(repo)}/issues/{number}/timeline"):
            if item.get("type") != _COMMENT_TYPE_LABEL or (item.get("label") or {}).get("name") != FACTORY_LABEL:
                continue
            if str(item.get("body")) != "1":  # measured on 28.2: "1" put on, "" taken off
                continue
            # A label from before the baseline isn't a request, even if the issue moves later.
            if parse_time(item.get("created_at")) < parse_time(baseline):
                continue
            events.append(
                Trigger(
                    source=self.name,
                    external_id=f"{repo}/issue/{number}/label/{item['id']}",
                    thread=issue_thread(repo, number),
                    kind=TriggerKind.ISSUE_LABELED,
                    actor=_login(item.get("user")),
                    repo=repo,
                    at=parse_time(item.get("created_at")),
                    data=self._issue_data(issue),
                )
            )
        return events

    @staticmethod
    def _issue_data(issue: dict) -> dict:
        return {
            "labels": _labels(issue),
            "title": issue.get("title") or "",
            "issue_body": issue.get("body") or "",
            "issue_author": _login(issue.get("user")),
            "number": issue["number"],
        }

    # --- comments ----------------------------------------------------------------------

    def _is_own(self, login: str, body: str) -> bool:
        policy = self.policy()
        if login not in policy.bots and login not in policy.reviewer_bots:
            return False
        # On a shared sandbox account the owner and the factory have one login; only the footer differs.
        return login != policy.sandbox_shared_account or has_footer(body)

    def _comments(self, repo: RepoPolicy, state: dict) -> list[Trigger]:
        path, events = repo_path(repo.name), []
        since = state["comments_since"]
        for comment in self.client.pages(f"{path}/issues/comments", since=since):
            state["comments_since"] = max(state["comments_since"], _utc(comment["updated_at"]))
            login, body = _login(comment.get("user")), comment.get("body") or ""
            if self._is_own(login, body):
                continue
            target = self._comment_target(comment)
            if target is None:
                continue
            kind, number = target
            thread = pr_thread(repo.name, number) if kind == "pr" else issue_thread(repo.name, number)
            data: dict[str, Any] = {"number": number, "comment": comment["id"]}
            if is_factory_command(body):  # §4.1: this may become a Ticket, which needs the issue
                issue = self.client.get_or_none(f"{path}/issues/{number}")
                if issue is not None:
                    data.update(self._issue_data(issue))
                    data["labels"] = []  # the comment is the request, not the label
            events.append(
                Trigger(
                    source=self.name,
                    external_id=f"{repo.name}/comment/{comment['id']}",
                    thread=thread,
                    kind=TriggerKind.COMMENT,
                    actor=login,
                    body=body,
                    repo=repo.name,
                    at=parse_time(comment.get("created_at")),
                    data=data,
                )
            )
        return events

    @staticmethod
    def _comment_target(comment: dict) -> tuple[str, int] | None:
        for kind, field in (("pr", "pull_request_url"), ("issue", "issue_url")):
            url = comment.get(field) or ""
            tail = url.rstrip("/").rsplit("/", 1)[-1]
            if url and tail.isdigit():
                return kind, int(tail)
        return None

    # --- PRs: reviews and CI --------------------------------------------------------------

    def _reviews(self, repo: str, number: int) -> list[dict]:
        return [r for r in self.client.pages(f"{repo_path(repo)}/pulls/{number}/reviews") if r.get("state") != "PENDING"]

    def _pulls(self, repo: RepoPolicy, state: dict) -> list[Trigger]:
        path, events, seen = repo_path(repo.name), [], state["prs"]
        current = {}
        for pr in self.client.pages(f"{path}/pulls", state="open"):
            number, updated = pr["number"], pr["updated_at"]
            current[str(number)] = updated
            recent = self._now() - parse_time(updated) < _SAME_SECOND_SECONDS
            if seen.get(str(number)) != updated or recent:
                events += self._pr_reviews(repo.name, number, state)
            events += self._ci_fact(repo, ci_thread(repo.name, number), pr["head"]["sha"], number)
        state["prs"] = current
        return events

    def _pr_reviews(self, repo: str, number: int, state: dict) -> list[Trigger]:
        known = set(state.get("reviews_seen") or ())
        events = []
        for review in self._reviews(repo, number):
            if review["id"] in known:
                continue
            login, body = _login(review.get("user")), self._review_text(repo, number, review)
            if self._is_own(login, review.get("body") or "") or review.get("dismissed"):
                continue
            events.append(
                Trigger(
                    source=self.name,
                    external_id=f"{repo}/review/{review['id']}",
                    thread=pr_thread(repo, number),
                    kind=TriggerKind.REVIEW,
                    actor=login,
                    body=body,
                    repo=repo,
                    at=parse_time(review.get("submitted_at")),
                    data={"number": number, "state": review.get("state"), "commit": review.get("commit_id")},
                )
            )
        return events

    def _review_text(self, repo: str, number: int, review: dict) -> str:
        """The review's words and its line comments. Empty means a bare approval: nothing to tell."""
        parts = [str(review.get("body") or "").strip()]
        if review.get("comments_count"):
            for item in self.client.pages(f"{repo_path(repo)}/pulls/{number}/reviews/{review['id']}/comments"):
                parts.append(f"{item.get('path')}:{item.get('position')}: {item.get('body')}")
        text = "\n".join(p for p in parts if p)
        if not text and review.get("state") == "REQUEST_CHANGES":
            return "(asked for changes, with no text)"
        return text

    def _ci_fact(self, repo: RepoPolicy, thread: str, sha: str, number: int | None) -> list[Trigger]:
        try:
            ci = self.ci(repo.name, sha, repo.required_checks, repo.ci_runner)
        except GiteaError as exc:
            log.warning("CI read on %s: %s", sha[:10], exc)
            return []
        state = _CI_FACT[ci.state]
        return [
            Trigger(
                source=self.name,
                external_id=f"{repo.name}/ci/{thread}/{sha}/{state}",
                thread=thread,
                kind=TriggerKind.CI_STATUS,
                actor="gitea",
                actor_kind=ActorKind.SYSTEM,
                repo=repo.name,
                at=self._now(),
                data={"state": state, "sha": sha, "number": number},
            )
        ]

    def _main(self, repo: RepoPolicy, state: dict) -> list[Trigger]:
        branch = self.client.get_or_none(f"{repo_path(repo.name)}/branches/{branch_path(repo.base)}")
        if branch is None:
            return []
        sha = branch["commit"]["id"]
        thread = main_ci_thread(repo.name)
        events = self._ci_fact(repo, thread, sha, None)
        if state.get("main") not in (None, sha):
            events.append(
                Trigger(
                    source=self.name,
                    external_id=f"{repo.name}/main/{sha}",
                    thread=thread,
                    kind=TriggerKind.MAIN_MOVED,
                    actor="gitea",
                    actor_kind=ActorKind.SYSTEM,
                    repo=repo.name,
                    at=self._now(),
                    data={"sha": sha},
                )
            )
        state["main"] = sha
        return events


def _utc(text: str) -> str:
    return format_time(parse_time(text))
