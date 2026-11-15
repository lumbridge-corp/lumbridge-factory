"""Worktrees and pre-trust (ARCHITECTURE §5.2).

One bare clone per repo under repos_dir, one git worktree per Assignment name under
worktrees_dir. `prepare` is idempotent: an existing worktree is kept as it is, so a
retry or a resume finds the builder's work.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import threading
from dataclasses import dataclass

from lumbridge_factory.core.executor import WorktreeSpec

from .childenv import base_env
from .config import Config

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
GIT_TIMEOUT = 600


class WorktreeError(Exception):
    def __init__(self, code: str, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def _bad(message: str) -> WorktreeError:
    return WorktreeError("bad_request", message, False)


@dataclass(frozen=True)
class Prepared:
    path: str
    common_dir: str
    branch: str


class Worktrees:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock(self, repo: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(repo, threading.Lock())

    def _git(self, *args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
        env = base_env(self.config)
        try:
            return subprocess.run(
                ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL
            )
        except subprocess.TimeoutExpired as exc:
            raise WorktreeError("unavailable", f"git {args[0]} timed out", True) from exc

    def _git_ok(self, *args: str, cwd: str | None = None, retryable: bool = False) -> str:
        out = self._git(*args, cwd=cwd)
        if out.returncode != 0:
            code = "unavailable" if retryable else "internal"
            raise WorktreeError(code, f"git {args[0]} failed: {out.stderr.strip()[-300:]}", retryable)
        return out.stdout.strip()

    def _clone_path(self, repo: str) -> str:
        return os.path.join(self.config.repos_dir, repo.replace("/", "__") + ".git")

    def _ensure_clone(self, repo: str) -> str:
        url = self.config.repos.get(repo)
        if not url:
            raise _bad(f"repo {repo} is not in this agentd's config")
        path = self._clone_path(repo)
        if not os.path.exists(os.path.join(path, "HEAD")):
            os.makedirs(path, exist_ok=True)
            self._git_ok("init", "-q", "--bare", path)
            self._git_ok("remote", "add", "origin", url, cwd=path)
            self._git_ok("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*", cwd=path)
        self._git_ok("fetch", "-q", "--prune", "origin", cwd=path, retryable=True)
        return path

    def prepare(self, spec: WorktreeSpec) -> Prepared:
        if not _REPO.match(spec.repo):
            raise _bad(f"bad repo name {spec.repo!r}")
        if not _NAME.match(spec.name):
            raise _bad(f"bad worktree name {spec.name!r}")
        if spec.pinned_sha is not None and not _SHA.match(spec.pinned_sha):
            raise _bad("pinned_sha must be a hex commit id")
        if spec.pinned_sha is None:
            if not _BRANCH.match(spec.branch or "") or ".." in spec.branch:
                raise _bad(f"bad branch name {spec.branch!r}")
            if not _BRANCH.match(spec.base or "") or ".." in spec.base:
                raise _bad(f"bad base name {spec.base!r}")
        os.makedirs(self.config.worktrees_dir, exist_ok=True)
        os.makedirs(self.config.repos_dir, exist_ok=True)
        path = os.path.join(self.config.worktrees_dir, spec.name)
        with self._lock(self._clone_path(spec.repo)):
            common = self._ensure_clone(spec.repo)
            if os.path.exists(path):
                self._check_existing(path, spec)
            else:
                self._git("worktree", "prune", cwd=common)
                if spec.pinned_sha:
                    self._add_pinned(common, path, spec)
                else:
                    self._add_branch(common, path, spec)
        if spec.read_only:
            make_read_only(path)
        return Prepared(path=path, common_dir=common, branch=spec.branch)

    def remove(self, path: str, common_dir: str, branch: str) -> None:
        """Idempotent. Only under worktrees_dir: the path came off disk, but a mistake here deletes."""
        root = os.path.realpath(self.config.worktrees_dir)
        real = os.path.realpath(path)
        if os.path.dirname(real) != root:
            raise WorktreeError("internal", f"{path} is not directly under the worktrees directory", False)
        with self._lock(common_dir):
            if os.path.lexists(real):
                make_writable(real)
                self._git("worktree", "remove", "--force", real, cwd=common_dir)
                if os.path.lexists(real):  # git didn't know it, or a file resisted
                    shutil.rmtree(real)
            self._git("worktree", "prune", cwd=common_dir)
            if branch:  # the pushed copy on origin is what survives
                self._git("branch", "-D", branch, cwd=common_dir)

    def _check_existing(self, path: str, spec: WorktreeSpec) -> None:
        if not os.path.exists(os.path.join(path, ".git")):
            raise WorktreeError("internal", f"{path} exists but isn't a worktree", False)
        if spec.pinned_sha:
            head = self._git_ok("rev-parse", "HEAD", cwd=path)
            if not head.startswith(spec.pinned_sha):
                raise _bad(f"worktree {spec.name} is pinned to another commit")

    def _add_pinned(self, common: str, path: str, spec: WorktreeSpec) -> None:
        self._git_ok("rev-parse", "--verify", "-q", f"{spec.pinned_sha}^{{commit}}", cwd=common, retryable=True)
        self._git_ok("worktree", "add", "-q", "--detach", path, spec.pinned_sha, cwd=common)

    def _add_branch(self, common: str, path: str, spec: WorktreeSpec) -> None:
        def exists(ref: str) -> bool:
            return self._git("rev-parse", "--verify", "-q", ref, cwd=common).returncode == 0

        if exists(f"refs/heads/{spec.branch}"):
            self._git_ok("worktree", "add", "-q", path, spec.branch, cwd=common)
        elif exists(f"refs/remotes/origin/{spec.branch}"):
            # The branch outlived its worktree: carry on from what was pushed.
            self._git_ok("worktree", "add", "-q", "-b", spec.branch, path, f"origin/{spec.branch}", cwd=common)
        elif exists(f"refs/remotes/origin/{spec.base}"):
            self._git_ok("worktree", "add", "-q", "--no-track", "-b", spec.branch, path, f"origin/{spec.base}", cwd=common)
        else:
            raise _bad(f"origin has no branch {spec.base}")


def make_read_only(path: str) -> None:
    """A reviewer's tree can't be edited by accident. The same user could chmod it back."""
    for root, dirs, files in os.walk(path):
        for name in files:
            full = os.path.join(root, name)
            if not os.path.islink(full):
                mode = stat.S_IMODE(os.lstat(full).st_mode)
                os.chmod(full, mode & ~0o222)
        for name in dirs:
            full = os.path.join(root, name)
            if not os.path.islink(full):
                mode = stat.S_IMODE(os.lstat(full).st_mode)
                os.chmod(full, mode & ~0o222)
    os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) & ~0o222)


def make_writable(path: str) -> None:
    """Undoes make_read_only, and whatever a build left read-only, so the tree can be deleted."""
    os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) | 0o700)
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            full = os.path.join(root, name)
            if os.path.islink(full):
                continue
            mode = stat.S_IMODE(os.lstat(full).st_mode)
            os.chmod(full, mode | (0o700 if os.path.isdir(full) else 0o600))


def forget_worktree(agent_home: str, worktree: str) -> None:
    """The CLI's own per-folder state for a tree that no longer exists: its session store and its
    pre-trust entry. A later session in the same path starts clean."""
    slug = re.sub(r"[^A-Za-z0-9]", "-", worktree)
    shutil.rmtree(os.path.join(agent_home, ".claude", "projects", slug), ignore_errors=True)
    path = os.path.join(agent_home, ".claude.json")
    with open(path + ".lock", "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, ValueError):
            return
        projects = data.get("projects") if isinstance(data, dict) else None
        if not isinstance(projects, dict) or worktree not in projects:
            return
        del projects[worktree]
        tmp = f"{path}.tmp.{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)


def pre_trust(agent_home: str, worktree: str) -> None:
    """Claude Code's per-folder trust and its first-run screens, in the agent's own home.

    Print mode skips the trust dialog, but `lumbridge-factory attach` (a person resuming the
    session in a pane) does not, and would stop at it.
    """
    path = os.path.join(agent_home, ".claude.json")
    with open(path + ".lock", "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                data = {}
        except FileNotFoundError:
            data = {}
        except ValueError:
            return  # the CLI is mid-write or it's damaged; never clobber it
        data.setdefault("hasCompletedOnboarding", True)
        data.setdefault("theme", "dark")
        data.setdefault("hasSeenAutoDefaultNudge", True)
        data.setdefault("hasSeenEffortMediumNudge", True)
        project = data.setdefault("projects", {}).setdefault(worktree, {})
        project["hasTrustDialogAccepted"] = True
        project["hasCompletedProjectOnboarding"] = True
        tmp = f"{path}.tmp.{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
