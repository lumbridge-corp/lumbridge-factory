"""What an instance declares (§2) and the rules it starts with (§4.4)."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from .model import RuleMode

# A policy with `sandbox_shared_account` may name only repositories called this, under any owner (§13).
SANDBOX_REPO_NAME = "factory-sandbox"


def is_sandbox_repo(name: str) -> bool:
    return name.rsplit("/", 1)[-1] == SANDBOX_REPO_NAME

ALWAYS_ON = frozenset({"build", "outbound-guard", "handback"})
PROACTIVE = frozenset({"main-red", "triage"})

DEFAULT_RULES = {
    "build": RuleMode.AUTO,
    "outbound-guard": RuleMode.AUTO,
    "handback": RuleMode.AUTO,
    "main-red": RuleMode.OBSERVE,
    "triage": RuleMode.OBSERVE,
}


# What Gitea allows in an owner or a repository name. Checked when a repository is added (with_repository), not in validate():
# factoryd validates the stored policy at every start, and a live store must keep starting.
_REPO_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class RepoPolicy:
    name: str
    base: str = "main"
    required_checks: tuple[str, ...] = ()
    ci_runner: str = ""  # the runner a green must come from; several, comma-separated, when CI has more than one
    # Optional, per repo: a passing review carries to a new head when the PR only picked up main and its
    # diff against main is unchanged; CI still runs again. Off unless a repo turns it on.
    carry_review_across_main: bool = False
    # What this repository adds to the instance's data rules: its own scratch data and what its tests must never touch. Shown to
    # workers on this repo's Tickets only, after the instance's rules.
    data_rules: str = ""


@dataclass(frozen=True)
class Policy:
    instance: str
    allowed: dict[str, tuple[str, ...]]  # source -> people who may ask for work
    repositories: tuple[RepoPolicy, ...]
    bots: tuple[str, ...]  # the factory's own accounts; never admitted
    reviewer_bots: tuple[str, ...]  # only these accounts' reviews count at gate 2
    data_rules: str
    outside_action_rules: str
    company_clauses: str
    merge_policy: str
    version: int = 1
    rules: dict[str, RuleMode] = field(default_factory=lambda: dict(DEFAULT_RULES))
    slots: int = 3
    reserved_slots: int = 1  # beyond `slots`, for turns caused by an allowed person (§7)
    # Optional: `slots`, but only `tight_slots` once `tight_account` has used `tight_pct` of its allowance (the accounts
    # provider's `used_pct`), or when nobody can say how much it has used. Empty `tight_account`: no fallback.
    tight_account: str = ""
    tight_pct: float = 70.0
    tight_slots: int = 2
    max_review_rounds: int = 3
    budget_tokens: int = 2_000_000
    budget_wall_seconds: int = 6 * 3600
    turn_limit_seconds: int = 45 * 60
    builder_model: str = "claude-opus-5-5"
    reviewer_model: str = "claude-sonnet-5-5"
    chat_reply_words: int = 100
    # SANDBOX ONLY. One real account plays the owner, the factory and the reviewer, because a sandbox
    # has no bot accounts. Validation refuses this on any repository not named SANDBOX_REPO_NAME. With it, an account's
    # own events can only be told apart by the footer the Channel writes (channels/gitea.py).
    sandbox_shared_account: str = ""

    def validate(self) -> None:
        """§2: refuse an instance that leaves anything undecided."""
        problems: list[str] = []
        if not self.instance:
            problems.append("instance name")
        if not any(self.allowed.values()):
            problems.append("who may ask for work")
        if not self.repositories:
            problems.append("repositories")
        names = [r.name for r in self.repositories]
        if len(set(names)) != len(names):
            problems.append("a repository named twice")
        for repo in self.repositories:
            if not repo.name or repo.name.count("/") != 1 or any(c.isspace() for c in repo.name) or "" in repo.name.split("/"):
                problems.append(f"repository name {repo.name!r} (owner/name)")
            if not repo.base.strip():
                problems.append(f"base branch for {repo.name}")
            if not repo.required_checks:
                problems.append(f"required checks for {repo.name}")
        if not self.bots:
            problems.append("the factory's own bot accounts")
        if not self.reviewer_bots:
            problems.append("reviewer bot accounts")
        for label, value in (
            ("data rules", self.data_rules),
            ("outside-action rules", self.outside_action_rules),
            ("company clauses", self.company_clauses),
            ("merge policy", self.merge_policy),
        ):
            if not value.strip():
                problems.append(label)
        shared = self.sandbox_shared_account
        if shared:
            outside = sorted(r.name for r in self.repositories if not is_sandbox_repo(r.name))
            if outside:
                problems.append(f"a shared account is for the sandbox only, not {outside}")
        people = {p for names in self.allowed.values() for p in names}
        bots = set(self.bots) | set(self.reviewer_bots)
        if (people & bots) - {shared}:
            problems.append(f"bot accounts in the allowed list: {sorted((people & bots) - {shared})}")
        if (set(self.bots) & set(self.reviewer_bots)) - {shared}:
            problems.append("the builder bot can't also be a reviewer bot")
        for rule in ALWAYS_ON:
            if self.rules.get(rule) != RuleMode.AUTO:
                problems.append(f"rule {rule} is always on")
        if self.slots < 1:
            problems.append("at least one slot")
        if self.tight_account and not 1 <= self.tight_slots <= self.slots:
            problems.append("tight_slots must be between 1 and slots")
        if self.tight_account and not 0 < self.tight_pct <= 100:
            problems.append("tight_pct must be a percentage")
        if problems:
            raise PolicyError("instance leaves undecided or wrong: " + "; ".join(problems))

    def repo(self, name: str) -> RepoPolicy | None:
        return next((r for r in self.repositories if r.name == name), None)

    def is_allowed(self, source: str, person: str) -> bool:
        if person not in self.allowed.get(source, ()):
            return False
        if self.sandbox_shared_account and person == self.sandbox_shared_account:
            return True  # the Source has already dropped what the factory itself wrote
        return person not in self.bots and person not in self.reviewer_bots

    def with_rule(self, rule: str, mode: RuleMode) -> "Policy":
        if rule not in self.rules:
            raise PolicyError(f"no such rule: {rule}")
        if rule in ALWAYS_ON and mode != RuleMode.AUTO:
            raise PolicyError(f"rule {rule} is always on")
        return replace(self, rules={**self.rules, rule: mode})

    def with_allowed(self, source: str, people: tuple[str, ...]) -> "Policy":
        return replace(self, allowed={**self.allowed, source: people})

    def with_slots(self, slots: int, tight_account: str, tight_pct: float, tight_slots: int) -> "Policy":
        return replace(self, slots=slots, tight_account=tight_account, tight_pct=tight_pct, tight_slots=tight_slots)

    def with_ci_runners(self, repo: str, names: tuple[str, ...]) -> "Policy":
        """The runners whose green counts for one repo. Never empty: a repo without a named runner trusts any."""
        names = tuple(n.strip() for n in names if n.strip())
        if self.repo(repo) is None:
            raise PolicyError(f"no such repository: {repo}")
        if not names or any("," in n or " " in n for n in names):
            raise PolicyError("name at least one runner, without commas or spaces")
        return replace(self, repositories=tuple(replace(r, ci_runner=",".join(names)) if r.name == repo else r for r in self.repositories))

    def with_carry_review(self, repo: str, on: bool) -> "Policy":
        """Gate 2's carried review for one repo (opt-in per repo)."""
        if self.repo(repo) is None:
            raise PolicyError(f"no such repository: {repo}")
        return replace(self, repositories=tuple(replace(r, carry_review_across_main=on) if r.name == repo else r for r in self.repositories))

    def with_repository(self, repo: RepoPolicy) -> "Policy":
        """One more repository (§2: everything a repo needs is stated with it). Refuses one already there, and one without a
        named CI runner: a repo without one would trust any runner's green."""
        # Gitea's names don't tell case apart, and Triggers carry the forge's spelling: a second spelling would never match one.
        if any(r.name.lower() == repo.name.lower() for r in self.repositories):
            raise PolicyError(f"{repo.name} is already in the policy")
        if not _REPO_NAME.fullmatch(repo.name) or {".", ".."} & set(repo.name.split("/")):
            raise PolicyError(f"{repo.name!r} is not an owner/name")
        if any(c.isspace() for c in repo.base):
            raise PolicyError(f"base branch {repo.base!r} has a space in it")
        if any(not c.strip() for c in repo.required_checks):
            raise PolicyError("a required check without a name would never be met")
        runners = tuple(n.strip() for n in repo.ci_runner.split(",") if n.strip())
        if not runners or any(c.isspace() for n in runners for c in n):
            raise PolicyError("name at least one runner, without commas or spaces")
        return replace(self, repositories=self.repositories + (replace(repo, ci_runner=",".join(runners)),))

    def without_repository(self, name: str) -> "Policy":
        """The repository leaves the policy. Whoever calls this checks that no open Ticket is on it (engine.check_removal)."""
        if self.repo(name) is None:
            raise PolicyError(f"no such repository: {name}")
        return replace(self, repositories=tuple(r for r in self.repositories if r.name != name))

    def with_repo_data_rules(self, name: str, text: str) -> "Policy":
        if self.repo(name) is None:
            raise PolicyError(f"no such repository: {name}")
        return replace(self, repositories=tuple(replace(r, data_rules=text.strip()) if r.name == name else r for r in self.repositories))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # An empty `data_rules` isn't stored, so a release from before it can still read the
        # policy once every repo's own rules are cleared again. That is the rollback path.
        d["repositories"] = [{k: v for k, v in r.items() if not (k == "data_rules" and not v)} for r in d["repositories"]]
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Policy":
        d = dict(d)
        d["allowed"] = {k: tuple(v) for k, v in d["allowed"].items()}
        d["repositories"] = tuple(
            RepoPolicy(**{**r, "required_checks": tuple(r["required_checks"])}) for r in d["repositories"]
        )
        d["bots"] = tuple(d["bots"])
        d["reviewer_bots"] = tuple(d["reviewer_bots"])
        d["rules"] = {k: RuleMode(v) for k, v in d["rules"].items()}
        return Policy(**d)


def default_policy(instance: str = "scratch", repo: str = "acme/sandbox") -> Policy:
    """The owner only, on every Source. A real instance states all of this in its directory."""
    return Policy(
        instance=instance,
        allowed={"gitea": ("owner",), "office": ("owner",)},
        repositories=(RepoPolicy(name=repo, required_checks=("CI / checks (pull_request)",)),),
        bots=(f"{instance}-factory",),
        reviewer_bots=(f"{instance}-review",),
        data_rules="No production database, personal mailbox or browser session; synthetic fixtures only.",
        outside_action_rules="Any outbound contact is stubbed off outside production and sits behind an approval.",
        company_clauses="None.",
        merge_policy="The owner merges.",
    )
