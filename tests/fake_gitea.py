"""A small Gitea over real HTTP, so the client, Source, Channel and Gates are tested through urllib.

Its shapes and quirks are the ones measured on git.example.com 28.2 (2026-11-04):
timeline label bodies "1" and "", `compare/<head>...<main>` counting what the head lacks, a repeated
branch update answering 500 "up to date", a conflict 409, a duplicate PR 409, a missing branch 404,
and no approving your own PR.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = "acme/factory-sandbox"


def stamp() -> str:
    return datetime.fromtimestamp(time.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha_of(*parts) -> str:
    return hashlib.sha1(":".join(str(p) for p in parts).encode()).hexdigest()


class FakeGitea:
    def __init__(self, tokens: dict[str, str] | None = None, git_dir: str | None = None):
        self.tokens = tokens or {"factory-token": "factory-bot", "review-token": "review-bot", "owner-token": "owner", "stranger-token": "stranger"}
        self.git_dir = git_dir  # if set, branches are read from this bare repo
        self.lock = threading.RLock()
        self.seq = 1000
        self.issues: dict[int, dict] = {}
        self.comments: list[dict] = []
        self.timeline: dict[int, list[dict]] = {}
        self.reviews: dict[int, list[dict]] = {}
        self.review_comments: dict[int, list[dict]] = {}
        self.statuses: dict[str, list[dict]] = {}
        self.branches: dict[str, str] = {"main": sha_of("main", 0)}
        self.contains: dict[str, set[str]] = {}  # head sha -> the main shas it contains
        self.runs: list[dict] = []
        self.jobs: dict[int, list[dict]] = {}
        self.logs: dict[int, str] = {}
        self.reruns: list[int] = []
        self.requests: list[tuple[str, str, str]] = []
        self.fail: dict[str, int] = {}  # path substring -> this many 503s
        self.down = False
        self.redirect_to: str | None = None
        self.update_conflicts = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # --- the world, changed directly by a test --------------------------------------

    def _id(self) -> int:
        self.seq += 1
        return self.seq

    def age(self, seconds: float = 3600.0) -> None:
        """Everything so far happened a while ago."""
        def back(text: str) -> str:
            when = datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() - seconds
            return datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        with self.lock:
            for issue in self.issues.values():
                issue["created_at"], issue["updated_at"] = back(issue["created_at"]), back(issue["updated_at"])
            for item in self.comments:
                item["created_at"], item["updated_at"] = back(item["created_at"]), back(item["updated_at"])
            for events in self.timeline.values():
                for event in events:
                    event["created_at"] = back(event["created_at"])

    def main_sha(self) -> str:
        return self.branch_sha("main")

    def branch_sha(self, branch: str) -> str | None:
        if self.git_dir:
            out = subprocess.run(["git", "rev-parse", "-q", "--verify", f"refs/heads/{branch}"], cwd=self.git_dir, capture_output=True, text=True)
            return out.stdout.strip() or None
        return self.branches.get(branch)

    def move_main(self) -> str:
        self.branches["main"] = sha_of("main", self.branches["main"])
        return self.branches["main"]

    def push(self, branch: str, sha: str | None = None) -> str:
        """A push to a branch. The new head contains current main, and any open PR's head follows."""
        with self.lock:
            sha = sha or sha_of(branch, self._id())
            self.branches[branch] = sha
            self.contains.setdefault(sha, set()).add(self.main_sha())
            for issue in self.issues.values():
                if issue.get("pull") and issue["head_ref"] == branch and issue["state"] == "open":
                    issue["head_sha"] = sha
                    issue["updated_at"] = stamp()
            return sha

    def issue(self, title="An issue", body="The body", author="owner", labels=(), labeler=None) -> int:
        with self.lock:
            number = max(self.issues, default=0) + 1
            self.issues[number] = {
                "number": number, "title": title, "body": body, "user": {"login": author}, "state": "open", "labels": [],
                "created_at": stamp(), "updated_at": stamp(),
            }
            self.timeline[number] = []
            for label in labels:
                self.label(number, label, labeler or author)
            return number

    def label(self, number: int, name: str, who: str) -> None:
        with self.lock:
            issue = self.issues[number]
            if name not in [l["name"] for l in issue["labels"]]:
                issue["labels"].append({"name": name})
            issue["updated_at"] = stamp()
            self.timeline[number].append({"id": self._id(), "type": "label", "body": "1", "label": {"name": name}, "user": {"login": who}, "created_at": stamp()})

    def unlabel(self, number: int, name: str, who: str) -> None:
        with self.lock:
            issue = self.issues[number]
            issue["labels"] = [l for l in issue["labels"] if l["name"] != name]
            issue["updated_at"] = stamp()
            self.timeline[number].append({"id": self._id(), "type": "label", "body": "", "label": {"name": name}, "user": {"login": who}, "created_at": stamp()})

    def comment(self, number: int, body: str, who: str = "owner") -> dict:
        with self.lock:
            issue = self.issues[number]
            field = "pull_request_url" if issue.get("pull") else "issue_url"
            other = "issue_url" if issue.get("pull") else "pull_request_url"
            kind = "pulls" if issue.get("pull") else "issues"
            item = {
                "id": self._id(), "body": body, "user": {"login": who}, "created_at": stamp(), "updated_at": stamp(),
                field: f"{self.url}/{REPO}/{kind}/{number}", other: "", "number": number,
            }
            self.comments.append(item)
            issue["updated_at"] = stamp()
            return item

    def open_pr(self, branch: str, title="A PR", body="", who="factory-bot", base="main", fork: str | None = None) -> int:
        with self.lock:
            sha = self.branch_sha(branch) or self.push(branch)
            number = max(self.issues, default=0) + 1
            self.issues[number] = {
                "number": number, "title": title, "body": body, "user": {"login": who}, "state": "open", "labels": [], "pull": True,
                "head_ref": branch, "head_sha": sha, "base_ref": base, "merged": False, "fork": fork, "created_at": stamp(), "updated_at": stamp(),
            }
            self.timeline[number] = []
            return number

    def set_state(self, number: int, state: str = "closed", merged: bool = False) -> None:
        with self.lock:
            self.issues[number].update(state=state, merged=merged, updated_at=stamp())

    def status(self, sha: str, state: str, context="CI / checks (pull_request)", description="", who="ci") -> None:
        with self.lock:
            self.statuses.setdefault(sha, []).append(
                {"id": self._id(), "status": state, "context": context, "description": description, "target_url": "", "creator": {"login": who}}
            )

    def review(self, number: int, body="", state="COMMENT", who="owner", commit=None, inline=()) -> dict:
        with self.lock:
            item = {
                "id": self._id(), "state": state, "body": body, "user": {"login": who}, "commit_id": commit or self.issues[number]["head_sha"],
                "comments_count": len(inline), "stale": False, "dismissed": False, "submitted_at": stamp(),
            }
            self.reviews.setdefault(number, []).append(item)
            self.review_comments[item["id"]] = [{"path": p, "position": line, "body": text, "user": {"login": who}} for p, line, text in inline]
            self.issues[number]["updated_at"] = stamp()
            return item

    def job(self, sha: str, name="checks", status="completed", conclusion="success", runner="r1", started=None, completed=None, log="") -> tuple[int, int]:
        with self.lock:
            run_id = self._id()
            self.runs.append({"id": run_id, "head_sha": sha})
            job = {"id": self._id(), "run_id": run_id, "name": name, "head_sha": sha, "status": status, "conclusion": conclusion,
                   "runner_name": runner, "started_at": started, "completed_at": completed}
            self.jobs[run_id] = [job]
            self.logs[job["id"]] = log
            return run_id, job["id"]

    # --- reading, for assertions -------------------------------------------------------

    def comments_on(self, number: int) -> list[str]:
        return [c["body"] for c in self.comments if c["number"] == number]

    def count(self, method: str, fragment: str) -> int:
        return sum(1 for m, path, _ in self.requests if m == method and fragment in path)

    # --- the API -------------------------------------------------------------------------

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body=None, headers=()):
                raw = b"" if body is None else (body.encode() if isinstance(body, str) else json.dumps(body).encode())
                self.send_response(status)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _run(self, method):
                parsed = urllib.parse.urlparse(self.path)
                query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                token = (self.headers.get("Authorization") or "").removeprefix("token ")
                who = fake.tokens.get(token)
                with fake.lock:
                    fake.requests.append((method, parsed.path, who or ""))
                    if fake.redirect_to:
                        return self._send(302, headers=(("Location", fake.redirect_to),))
                    if fake.down:
                        return self._send(503, {"message": "down"})
                    for fragment, left in list(fake.fail.items()):
                        if fragment in parsed.path and left > 0:
                            fake.fail[fragment] = left - 1
                            return self._send(503, {"message": "try later"})
                    if who is None:
                        return self._send(401, {"message": "bad token"})
                    path = parsed.path.removeprefix("/api/v1")
                    if path == "/user":
                        return self._send(200, {"login": who})
                    prefix = f"/repos/{REPO}"
                    if not path.startswith(prefix):
                        return self._send(404, {"message": "not found"})
                    return fake._api(self, method, path[len(prefix):], query, body, who)

            def do_GET(self):
                self._run("GET")

            def do_POST(self):
                self._run("POST")

        return Handler

    def _page(self, items, query):
        limit, page = int(query.get("limit", 50)), int(query.get("page", 1))
        return items[(page - 1) * limit : page * limit]

    def _issue_json(self, issue):
        return {k: v for k, v in issue.items() if k not in ("head_ref", "head_sha", "base_ref", "pull", "fork")}

    def _pull_json(self, issue):
        if self.git_dir and issue["state"] == "open":
            issue["head_sha"] = self.branch_sha(issue["head_ref"]) or issue["head_sha"]
        return {
            **self._issue_json(issue),
            "head": {"ref": issue["head_ref"], "sha": issue["head_sha"], "repo": {"full_name": issue.get("fork") or REPO}},
            "base": {"ref": issue["base_ref"], "repo": {"full_name": REPO}},
        }

    def _api(self, h, method, path, query, body, who):
        match = lambda pattern: re.fullmatch(pattern, path)
        since = query.get("since")
        if method == "GET" and path == "":
            return h._send(200, {"full_name": REPO})
        if method == "GET" and path == "/issues":
            items = [i for i in self.issues.values() if not i.get("pull")]
            state = query.get("state", "open")
            if state != "all":
                items = [i for i in items if i["state"] == state]
            if "labels" in query:
                items = [i for i in items if query["labels"] in [l["name"] for l in i["labels"]]]
            if since:
                items = [i for i in items if i["updated_at"] >= since]
            return h._send(200, [self._issue_json(i) for i in self._page(items, query)])
        if method == "GET" and path == "/issues/comments":
            items = [c for c in self.comments if not since or c["updated_at"] >= since]
            return h._send(200, self._page(items, query))
        if m := match(r"/issues/(\d+)"):
            issue = self.issues.get(int(m[1]))
            return h._send(200, self._issue_json(issue)) if issue else h._send(404, {"message": "not found"})
        if m := match(r"/issues/(\d+)/timeline"):
            return h._send(200, self._page(self.timeline.get(int(m[1]), []), query))
        if m := match(r"/issues/(\d+)/comments"):
            number = int(m[1])
            if method == "POST":
                return h._send(201, self.comment(number, body["body"], who))
            return h._send(200, self._page([c for c in self.comments if c["number"] == number], query))
        if path == "/pulls":
            if method == "GET":
                items = [i for i in self.issues.values() if i.get("pull") and (query.get("state", "open") == "all" or i["state"] == query.get("state", "open"))]
                return h._send(200, [self._pull_json(i) for i in self._page(items, query)])
            if self.branch_sha(body["head"]) is None:
                return h._send(404, {"message": f'ref "{body["head"]}" does not exist'})
            if self.branch_sha(body["head"]) == self.branch_sha(body["base"]):
                return h._send(422, {"message": "no commits between"})
            for i in self.issues.values():
                if i.get("pull") and i["state"] == "open" and i["head_ref"] == body["head"] and i["base_ref"] == body["base"]:
                    return h._send(409, {"message": "pull request already exists for these targets"})
            return h._send(201, self._pull_json(self.issues[self.open_pr(body["head"], body["title"], body.get("body", ""), who, body["base"])]))
        if m := match(r"/pulls/(\d+)"):
            issue = self.issues.get(int(m[1]))
            return h._send(200, self._pull_json(issue)) if issue and issue.get("pull") else h._send(404, {"message": "not found"})
        if m := match(r"/pulls/(\d+)/update"):
            issue = self.issues[int(m[1])]
            if self.update_conflicts:
                return h._send(409, {"message": "merge failed because of conflict"})
            if self.main_sha() in self.contains.get(issue["head_sha"], set()):
                return h._send(500, {"message": f"HeadBranch of PR {m[1]} is up to date"})
            self.push(issue["head_ref"])
            return h._send(200)
        if m := match(r"/pulls/(\d+)/reviews"):
            return h._send(200, self._page(self.reviews.get(int(m[1]), []), query))
        if m := match(r"/pulls/(\d+)/reviews/(\d+)/comments"):
            return h._send(200, self.review_comments.get(int(m[2]), []))
        if m := match(r"/commits/([0-9a-f]+)/statuses"):
            return h._send(200, self._page(list(reversed(self.statuses.get(m[1], []))), query))
        if m := match(r"/branches/(.+)"):
            sha = self.branch_sha(urllib.parse.unquote(m[1]))
            return h._send(200, {"commit": {"id": sha}}) if sha else h._send(404, {"message": "not found"})
        if m := match(r"/compare/([0-9a-f]+)\.\.\.([0-9a-f]+)"):
            if self.git_dir:
                check = subprocess.run(["git", "merge-base", "--is-ancestor", m[2], m[1]], cwd=self.git_dir, capture_output=True)
                lacking = check.returncode
            else:
                lacking = 0 if m[2] in self.contains.get(m[1], set()) else 1
            return h._send(200, {"total_commits": lacking, "commits": []})
        if path == "/actions/runs":
            runs = [r for r in self.runs if r["head_sha"] == query.get("head_sha")]
            return h._send(200, {"workflow_runs": runs, "total_count": len(runs)})
        if m := match(r"/actions/runs/(\d+)/jobs"):
            return h._send(200, {"jobs": self.jobs.get(int(m[1]), []), "total_count": 1})
        if m := match(r"/actions/runs/(\d+)/jobs/(\d+)/rerun"):
            self.reruns.append(int(m[2]))
            for job in self.jobs.get(int(m[1]), []):
                job.update(status="waiting", conclusion=None, started_at=stamp(), completed_at=None)
                self.status(job["head_sha"], "pending")
            return h._send(201, {})
        if m := match(r"/actions/jobs/(\d+)/logs"):
            return h._send(200, self.logs.get(int(m[1]), ""))
        return h._send(404, {"message": f"no route {method} {path}"})
