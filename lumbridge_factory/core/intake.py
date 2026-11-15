"""The gate (§4.1): the code path that decides what a Trigger may do. No model, no I/O.

Who wrote something is decided here. What they wrote is matched only against a few exact
commands; everything else in it stays data.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Sequence

from .model import ActorKind, PERSON_KINDS, SubMode, Trigger, TriggerKind, main_ci_thread
from .policy import Policy


class Route(StrEnum):
    NOT_ADMITTED = "not_admitted"  # recorded, never acted on
    IGNORE = "ignore"
    ACCEPT = "accept"  # explicit: a Ticket, no model
    FOLLOW_UP = "follow_up"  # to the Ticket(s) on this Thread, no Eval
    STOP = "stop"
    COMMAND = "command"  # hold, release, retry on a named Ticket
    CANDIDATE = "candidate"  # inferred: goes to the Router
    FACT = "fact"  # a Source's report about the world; prompts the gates


@dataclass(frozen=True)
class SubRef:
    ticket_id: str
    mode: SubMode
    stopped: bool
    ended: bool = False


@dataclass(frozen=True)
class GateResult:
    route: Route
    rule: str = ""
    reason: str = ""


STOP_WORDS = frozenset({"stop", "/factory stop"})
LOCAL_SOURCES = frozenset({"cli"})


def is_stop(text: str) -> bool:
    return text.strip().rstrip(".!").lower() in STOP_WORDS


def is_factory_command(text: str) -> bool:
    words = text.split()
    return bool(words) and words[0] == "/factory"


def gate(trigger: Trigger, policy: Policy, subs: Sequence[SubRef]) -> GateResult:
    if trigger.kind in PERSON_KINDS:
        return _gate_person(trigger, policy, subs)
    return _gate_fact(trigger, policy, subs)


def _gate_fact(trigger: Trigger, policy: Policy, subs: Sequence[SubRef]) -> GateResult:
    if trigger.actor_kind != ActorKind.SYSTEM:
        return GateResult(Route.NOT_ADMITTED, reason="a fact must come from a Source, not a person")
    if policy.repo(trigger.repo) is None and trigger.kind != TriggerKind.TIMER:
        return GateResult(Route.NOT_ADMITTED, reason="repository not in the policy")
    if (
        trigger.kind == TriggerKind.CI_STATUS
        and trigger.thread == main_ci_thread(trigger.repo)
        and trigger.data.get("state") == "red"
        and not any(not s.ended for s in subs)  # a Ticket is already on it
    ):
        return GateResult(Route.CANDIDATE, rule="main-red")
    return GateResult(Route.FACT)


def _gate_person(trigger: Trigger, policy: Policy, subs: Sequence[SubRef]) -> GateResult:
    if trigger.kind == TriggerKind.COMMAND and trigger.source in LOCAL_SOURCES:
        pass  # the local CLI: whoever can write the spool already runs this factory
    elif trigger.actor_kind != ActorKind.PERSON:
        return GateResult(Route.NOT_ADMITTED, reason="bots are not admitted")
    elif not policy.is_allowed(trigger.source, trigger.actor):
        return GateResult(Route.NOT_ADMITTED, reason=f"{trigger.actor} may not ask for work on {trigger.source}")
    command = trigger.data.get("command") if trigger.kind == TriggerKind.COMMAND else None
    if (trigger.kind != TriggerKind.COMMAND or command == "start") and policy.repo(trigger.repo) is None:
        return GateResult(Route.NOT_ADMITTED, reason="repository not in the policy")

    owners = [s for s in subs if s.mode == SubMode.OWNER and not s.stopped]
    context = [s for s in subs if s.mode == SubMode.CONTEXT and not s.stopped]

    if command == "stop":
        return GateResult(Route.STOP)
    if command in ("hold", "release", "retry"):
        return GateResult(Route.COMMAND)
    if trigger.kind in (TriggerKind.COMMENT, TriggerKind.REVIEW) and is_stop(trigger.body):
        return GateResult(Route.STOP) if owners else GateResult(Route.IGNORE, reason="nothing to stop")

    explicit = (
        command == "start"
        or (trigger.kind == TriggerKind.COMMENT and is_factory_command(trigger.body))
        or (
            trigger.kind in (TriggerKind.ISSUE_OPENED, TriggerKind.ISSUE_LABELED)
            and "factory" in trigger.data.get("labels", ())
        )
    )
    if owners or context:
        if not trigger.body.strip():
            return GateResult(Route.IGNORE, reason="nothing to tell the builder")  # a label, a repeated Start
        return GateResult(Route.FOLLOW_UP)
    if explicit:
        return GateResult(Route.ACCEPT, rule="build")
    if trigger.kind == TriggerKind.ISSUE_OPENED:
        return GateResult(Route.CANDIDATE, rule="triage")
    return GateResult(Route.IGNORE, reason="no Ticket on this Thread and no request")
