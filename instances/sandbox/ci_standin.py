#!/usr/bin/env python3
"""NOT REAL CI. The sandbox has no Gitea runner, so this plays one, on the sandbox repository only.

For each open PR's head that has no verdict yet, it checks that exact commit out from origin, runs its
`./check.sh`, and posts the required commit status with the same check name a real runner would. It
also applies one rule that isn't in the repo, so a builder can really meet a red it couldn't foresee:
every new line in CHANGES.md must start with "#<issue number>: ".

    ci_standin.py --state ~/factory-sandbox/ci [--once]

Sandbox only, by construction: the repo name is fixed here.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from lumbridge_factory.gitea_api import GiteaClient  # noqa: E402

REPO = "acme/factory-sandbox"
CONTEXT = "CI / checks (pull_request)"
TOKEN = os.path.expanduser("~/.config/gitea/token-issues")
URL = "ssh://git@git.example.com/acme/factory-sandbox.git"


def run(*argv: str, cwd: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


def post(client: GiteaClient, sha: str, state: str, description: str) -> None:
    client.post(f"/repos/{REPO}/statuses/{sha}", {"state": state, "context": CONTEXT, "description": description[:240], "target_url": ""})
    print(f"{time.strftime('%H:%M:%S')} {sha[:10]} {state}: {description[:120]}", flush=True)


def ci_only_rule(tree: str, base: str) -> str:
    diff = run("git", "diff", f"{base}...HEAD", "--", "CHANGES.md", cwd=tree).stdout
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++") and line[1:].strip() and not re.match(r"#\d+: ", line[1:]):
            return f"FAIL ci-lint: CHANGES.md: each new line must start with '#<issue number>: ', got: {line[1:80]}"
    return ""


def check(client: GiteaClient, sha: str, state_dir: str) -> None:
    post(client, sha, "pending", "Stand-in CI is running ./check.sh")
    clone = os.path.join(state_dir, "clone")
    if not os.path.isdir(clone):
        run("git", "clone", "-q", URL, clone).check_returncode()
    run("git", "fetch", "-q", "--prune", "origin", cwd=clone).check_returncode()
    run("git", "reset", "-q", "--hard", cwd=clone)
    run("git", "clean", "-qfdx", cwd=clone)
    out = run("git", "checkout", "-q", "--detach", sha, cwd=clone)
    if out.returncode != 0:
        post(client, sha, "failure", f"can't check out: {out.stderr[-200:]}")
        return
    tests = run("sh", "./check.sh", cwd=clone)
    if tests.returncode != 0:
        tail = (tests.stdout + tests.stderr).strip().splitlines()[-4:]
        post(client, sha, "failure", "FAIL ./check.sh: " + " | ".join(tail))
        return
    problem = ci_only_rule(clone, "origin/main")
    post(client, sha, "failure" if problem else "success", problem or "./check.sh passed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    state_dir = os.path.expanduser(args.state)
    os.makedirs(state_dir, exist_ok=True)
    client = GiteaClient(os.environ.get("GITEA_URL", "https://git.example.com"), TOKEN)
    while True:
        try:
            for pr in client.pages(f"/repos/{REPO}/pulls", state="open"):
                sha = pr["head"]["sha"]
                seen = client.get(f"/repos/{REPO}/commits/{sha}/statuses", limit=50) or []
                if not any(s.get("context") == CONTEXT for s in seen):
                    check(client, sha, state_dir)
        except Exception as exc:  # noqa: BLE001 - a stand-in that outlives a blip
            print(f"{time.strftime('%H:%M:%S')} error: {type(exc).__name__}: {exc}", flush=True)
        if args.once:
            return 0
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
