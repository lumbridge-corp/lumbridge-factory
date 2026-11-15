"""A Ticket's life (§5): the state machine. Pure: (Ticket, event) -> Transition.

Nothing here touches the world. A Transition names what must happen, and the engine stores
all of it in one transaction; outside effects run after commit.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace

from . import cards, prompts
from .executor import InboxEntry, InputKind, Report, ReportStatus, Role, TurnOutcome, TurnResult, Verdict
from .facts import CiState, GateFacts, PrState, ReviewState
from .model import (
    ActorKind,
    EndReason,
    Mandate,
    Phase,
    SubMode,
    Ticket,
    TicketStatus,
    Trigger,
    TriggerKind,
    WaitReason,
    ci_thread,
    clamp_words,
    is_chat_thread,
    pr_thread,
)
from .policy import Policy

MAX_TRANSIENT = 3
MAX_RECOVERIES = 5
MAX_STRIKES = 2


@dataclass(frozen=True)
class NewInput:
    kind: InputKind
    text: str
    ref: str = ""
    wake: bool = True
    urgent: bool = False


@dataclass(frozen=True)
class EffectSpec:
    kind: str
    key: str  # derived from the inputs, so a retry or a replay is the same effect
    payload: dict


@dataclass(frozen=True)
class Transition:
    ticket: Ticket
    effects: tuple[EffectSpec, ...] = ()
    inputs: tuple[NewInput, ...] = ()  # for the live builder
    subscribe: tuple[tuple[str, SubMode], ...] = ()
    unsubscribe: tuple[str, ...] = ()
    triggers: tuple[Trigger, ...] = ()  # the engine's own, recorded on their Threads
    start_reviewer_sha: str | None = None
    end_assignment: str | None = None
    replace_builder: bool = False
    stop_work: bool = False  # kill turns, cancel launches, drop queued inputs
    drop_system_inputs: bool = False
    requeue_inputs: bool = False  # the cut-off turn's inputs go out again


# --- comments ---------------------------------------------------------------------


def comment(t: Ticket, purpose: str, text: str, thread: str | None = None) -> EffectSpec:
    return EffectSpec(
        kind="comment",
        key=f"comment:{t.id}:{purpose}",
        payload={"thread": thread or t.origin_thread, "text": text, "ticket": t.id},
    )


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _ask(t: Ticket, reason: WaitReason, question: str, purpose: str, *more: EffectSpec) -> Transition:
    """One open question, posted where the owner made the request. Nothing is killed."""
    asked = replace(t, phase=Phase.WAITING, waiting_reason=reason, question=question)
    return Transition(asked, effects=(comment(t, purpose, question), *more))


# --- accept -----------------------------------------------------------------------


def new_ticket(m: Mandate, title: str) -> Ticket:
    return Ticket(
        id=m.ticket_id,
        repo=m.repo,
        base=m.base,
        branch=f"factory/{m.ticket_id}",
        title=title,
        origin_thread=m.origin_thread,
        mandate_id=m.id,
        requested_by=m.requested_by,
        created_at=m.created_at,
        budget_tokens=m.budget_tokens,
        budget_wall_seconds=m.budget_wall_seconds,
        base_budget_tokens=m.budget_tokens,
        base_budget_wall_seconds=m.budget_wall_seconds,
    )


def accepted(t: Ticket, trigger: Trigger, rule: str, note: str = "", origin_mode: SubMode = SubMode.OWNER) -> Transition:
    """§5.1: one sentence naming what it will do. An inferred Ticket stays quiet (§4.4)."""
    assignment = assignment_text(t, trigger, note)
    effects: tuple[EffectSpec, ...] = ()
    if rule == "build":
        title = cards.one_line(t.title) or "this issue"
        effects = (comment(t, "ack", f"Factory is on it: building \"{title}\" as ticket {t.id}, on branch {t.branch}."),)
    return Transition(
        t,
        effects=effects,
        inputs=(NewInput(InputKind.ASSIGNMENT, assignment, ref=trigger.id, urgent=rule == "build"),),
        subscribe=((t.origin_thread, origin_mode),),
    )


def _thread_name(thread: str) -> str:
    """Trusted text a builder needs and can't get: it has no forge access to look the number up."""
    tail = thread.rsplit("#", 1)[-1]
    kind, _, number = tail.partition("/")
    return f"{kind} #{number}" if number.isdigit() else tail


def assignment_text(t: Ticket, trigger: Trigger, note: str = "") -> str:
    parts = [f"Ticket {t.id} on {t.repo}, for {_thread_name(t.origin_thread)}. Your branch is {t.branch}, from {t.base}. Requested by {t.requested_by}."]
    if note:
        parts.append(f"Routing note: {clamp_words(note, 30)}")
    title = trigger.data.get("title") or ""
    author = str(trigger.data.get("author") or trigger.actor)
    issue_author = str(trigger.data.get("issue_author") or author)
    if title:  # the issue's writer, who needn't be the person who asked for it
        parts.append(cards.fence(title, author=issue_author, kind="title", ref=trigger.id))
    if trigger.data.get("issue_body"):
        parts.append(
            cards.spec_card(
                str(trigger.data["issue_body"]),
                author=issue_author,
                kind="issue",
                ref=trigger.id,
            )
        )
    if trigger.body:
        parts.append(cards.spec_card(trigger.body, author=author, kind=str(trigger.kind), ref=trigger.id))
    return "\n\n".join(parts)


def rule_prompt(rule: str) -> str:
    return prompts.RULE_INSTRUCTIONS.get(rule, "")


# --- The owner writes --------------------------------------------------------------


def _extend_budget(t: Ticket) -> Ticket:
    """The owner's word is the answer to a budget question: one more budget from here."""
    if not t.over_budget:
        return t
    return replace(
        t,
        budget_tokens=t.tokens + t.base_budget_tokens,
        budget_wall_seconds=int(t.wall_seconds) + t.base_budget_wall_seconds,
    )


def _reopen(t: Ticket) -> Ticket:
    """Clear whatever was waiting on the owner; they have now spoken."""
    t = _extend_budget(t)
    rounds = 0 if t.waiting_reason == WaitReason.ROUNDS else t.review_rounds
    return replace(
        t,
        status=TicketStatus.OPEN,
        end_reason=None,
        phase=Phase.BUILDING,
        accepted_sha=None,
        waiting_reason=None,
        question=None,
        review_rounds=rounds,
        strikes=0,
        recoveries=0,
        transient=0,
        next_turn_at=0.0,
        worktree_gone=False,
    )


def follow_up(t: Ticket, trigger: Trigger) -> Transition:
    """§5 Follow-ups and §5.8: the owner wrote on one of this Ticket's Threads."""
    if t.stopped:
        return Transition(t)
    resumed = not t.is_open
    note: tuple[NewInput, ...] = ()
    if resumed:
        pr_text = f"PR #{t.last_pr} is {t.end_reason}. " if t.last_pr else ""
        where = "a new worktree from your pushed branch, same Mandate" if t.worktree_gone else "same worktree, same session, same Mandate"
        note = (
            NewInput(
                InputKind.NOTE,
                f"This Ticket had ended and is now resumed: {where}. {pr_text}"
                "If you change code, sync with main, push and report ready.",
                urgent=True,
            ),
        )
    text = cards.spec_card(
        trigger.body,
        author=trigger.actor,
        kind=str(trigger.kind),
        ref=trigger.id,
        neighbours=(t.id, t.origin_thread),
    )
    # The owner can overrule a reviewer without a new commit, and the old verdict mustn't block for good.
    # The acceptance rule stands: a fresh independent review, which now reads his words too.
    overruled = t.changes_sha is not None and t.changes_sha == t.head_sha
    reopened = replace(_reopen(t), rereview=True, changes_sha=None) if overruled else _reopen(t)
    return Transition(
        reopened,
        inputs=(*note, NewInput(InputKind.TRIGGER, text, ref=trigger.id, urgent=True)),
        replace_builder=t.worktree_gone,
    )


def context_only(t: Ticket, trigger: Trigger) -> Transition:
    """A subscribed Thread the Ticket merely follows: goes with the next turn, wakes nothing."""
    if t.stopped:
        return Transition(t)
    text = cards.card(trigger.body, author=trigger.actor, kind=str(trigger.kind), ref=trigger.id)
    return Transition(t, inputs=(NewInput(InputKind.TRIGGER, text, ref=trigger.id, wake=False),))


def stop(t: Ticket, actor: str) -> Transition:
    """§5.8: withdraws the Mandate for good."""
    if t.stopped:
        return Transition(t)
    stopped = replace(
        t,
        status=TicketStatus.ENDED,
        end_reason=EndReason.STOPPED,
        stopped=True,
        held=False,
        waiting_reason=None,
        question=None,
    )
    return Transition(
        stopped, effects=(comment(t, "stopped", f"Stopped ticket {t.id}, as {actor} asked."),), stop_work=True
    )


def hold(t: Ticket) -> Transition:
    """No new turn starts. A running turn finishes; input queues until release."""
    return Transition(replace(t, held=True))


def release(t: Ticket) -> Transition:
    return Transition(replace(t, held=False))


def heard_meanwhile(t: Ticket) -> Transition:
    """The answer to the question just posted arrived before it did; it's already in the builder's queue."""
    return Transition(_reopen(t))


def retry(t: Ticket) -> Transition:
    """An explicit go from the owner: clears a question or a quarantine and tries again."""
    if t.stopped or not t.is_open:
        return Transition(t)
    reopened = replace(
        _reopen(t), held=False, quarantined=False, review_requested_sha=None, changes_sha=None, ci_red_sha=None, update_requested=None
    )
    if t.phase in (Phase.GATING, Phase.READY) and not t.quarantined:
        return Transition(replace(reopened, phase=t.phase))
    return Transition(
        reopened,
        inputs=(NewInput(InputKind.NOTE, "The owner asked you to try again. Check where things stand and continue.", urgent=True),),
    )


def quarantine(t: Ticket, what: str) -> Transition:
    """§8: only for an outside write whose outcome can't be checked."""
    question = f"I can't tell whether this happened: {what}. Check it, then run `lumbridge-factory retry {t.id}`."
    return Transition(replace(t, quarantined=True, question=question))


# --- a turn ended -----------------------------------------------------------------


def last_terminal_report(entries: tuple[InboxEntry, ...]) -> Report | None:
    """The newest report that says more than `progress`."""
    for entry in reversed(entries):
        if entry.report is not None and entry.report.status != ReportStatus.PROGRESS:
            return entry.report
    return None


def turn_ended(
    t: Ticket, role: Role, assignment_id: str, pinned_sha: str | None, result: TurnResult, report: Report | None, now: float
) -> Transition:
    t = replace(t, tokens=t.tokens + result.tokens, wall_seconds=t.wall_seconds + result.wall_seconds)
    if t.stopped or not t.is_open or result.outcome == TurnOutcome.STOPPED:
        return Transition(t, end_assignment=assignment_id if role == Role.REVIEWER else None)

    outcome = result.outcome
    if outcome in (TurnOutcome.INTERRUPTED, TurnOutcome.TIMEOUT):
        return _recover(t, role, assignment_id, outcome)
    if outcome == TurnOutcome.TRANSIENT:
        if t.transient < MAX_TRANSIENT:
            delay = 2.0 * (2**t.transient)
            return Transition(replace(t, transient=t.transient + 1, next_turn_at=now + delay), requeue_inputs=True)
        return _turn_trouble(t, role, assignment_id, f"it kept failing with provider errors: {result.error}")
    if outcome == TurnOutcome.ACCOUNT_LIMIT:
        return Transition(t, requeue_inputs=True)  # the engine marks the account; the turn waits for another
    if outcome == TurnOutcome.RESUME_FAILED:
        return Transition(replace(t, transient=0), requeue_inputs=True, replace_builder=role == Role.BUILDER)
    if outcome == TurnOutcome.FAILED:
        return _turn_trouble(t, role, assignment_id, f"the turn failed: {result.error}")

    t = replace(t, transient=0, recoveries=0)
    if role == Role.REVIEWER:
        return _reviewer_done(t, assignment_id, pinned_sha, report)
    return _builder_done(t, report)


def _recover(t: Ticket, role: Role, assignment_id: str, outcome: TurnOutcome) -> Transition:
    """§8: same session, a note, and the inputs again. Never a quarantine."""
    recoveries = t.recoveries + 1
    if recoveries > MAX_RECOVERIES:
        return _turn_trouble(replace(t, recoveries=0), role, assignment_id, f"{MAX_RECOVERIES} turns in a row were cut off")
    note = prompts.TIMEOUT_NOTE if outcome == TurnOutcome.TIMEOUT else prompts.RECOVERY_NOTE
    return Transition(replace(t, recoveries=recoveries), requeue_inputs=True, inputs=(NewInput(InputKind.RECOVERY, note),) if role == Role.BUILDER else ())


def _turn_trouble(t: Ticket, role: Role, assignment_id: str, why: str) -> Transition:
    question = f"Ticket {t.id}: {why}. Should I try again? Reply here, or run `lumbridge-factory retry {t.id}`."
    tr = _ask(replace(t, transient=0), WaitReason.TROUBLE, question, f"trouble:{_digest(why)}")
    return replace(tr, end_assignment=assignment_id if role == Role.REVIEWER else None, requeue_inputs=role == Role.BUILDER)


def _strike(t: Ticket, why: str, purpose: str) -> Transition:
    """A turn ended with nothing usable: one reminder, then ask."""
    strikes = t.strikes + 1
    if strikes >= MAX_STRIKES:
        question = f"Ticket {t.id}: the builder keeps ending turns without a usable report ({why}). What should it do?"
        return _ask(replace(t, strikes=0), WaitReason.TROUBLE, question, f"strikes:{purpose}")
    return Transition(
        replace(t, strikes=strikes, phase=Phase.BUILDING), inputs=(NewInput(InputKind.NOTE, prompts.NO_REPORT_NOTE),)
    )


def _builder_done(t: Ticket, report: Report | None) -> Transition:
    if report is None:
        return _strike(t, "no report", "none")
    status = report.status
    if status == ReportStatus.READY or (status == ReportStatus.WAITING and not report.question and (report.pr or t.pr)):
        number = report.pr or t.pr
        if number is None:
            return _open_pr(t, report)
        claim = None if number == t.pr else number
        return Transition(replace(t, phase=Phase.GATING, pr_claim=claim, strikes=0))
    if status in (ReportStatus.BLOCKED, ReportStatus.WAITING):
        question = report.question or report.summary
        if not question:
            return _strike(t, f"{status} without a question", "no-question")
        return _ask(replace(t, strikes=0), WaitReason.QUESTION, question, f"question:{_digest(question)}")
    if status == ReportStatus.FAILED:
        question = f"Ticket {t.id}: the builder says it failed: {report.summary}. What should it do?"
        return _ask(replace(t, strikes=0), WaitReason.TROUBLE, question, f"failed:{_digest(report.summary)}")
    if status == ReportStatus.DROPPED:
        text = f"Ticket {t.id} ended: the builder dropped it. {report.summary}".strip()
        ended = replace(t, status=TicketStatus.ENDED, end_reason=EndReason.DROPPED, strikes=0)
        return Transition(ended, effects=(comment(t, f"dropped:{_digest(text)}", text),), drop_system_inputs=True)
    return _strike(t, "progress only", "progress")


def _open_pr(t: Ticket, report: Report) -> Transition:
    """§5.3: a worker has no forge token, so factoryd opens the PR. It looks for an open one first,
    and the forge's answer is checked like any other claim."""
    opens = t.pr_opens + 1
    issue = re.fullmatch(r".*#issue/(\d+)", t.origin_thread)
    refs = f"\n\nRefs #{issue.group(1)}." if issue else ""
    effect = EffectSpec(
        kind="ensure_pr",
        key=f"ensure_pr:{t.id}:{opens}",
        payload={
            "ticket": t.id,
            "repo": t.repo,
            "branch": t.branch,
            "base": t.base,
            "title": cards.one_line(t.title) or t.branch,
            "body": f"{report.summary}{refs}\n\nBuilt by factory ticket {t.id}.",
        },
    )
    return Transition(replace(t, phase=Phase.GATING, pr_opens=opens), effects=(effect,))


def pr_opened(t: Ticket, number: int) -> Transition:
    """Only the forge's number, and gate_step still verifies it like any claim."""
    if t.stopped or not t.is_open or t.pr == number:
        return Transition(t)
    return Transition(replace(t, pr_claim=number, strikes=0))


def pr_refused(t: Ticket, why: str) -> Transition:
    """The builder said ready, but there is nothing to open a PR from."""
    if t.stopped or not t.is_open:
        return Transition(t)
    strikes = t.strikes + 1
    if strikes >= MAX_STRIKES:
        question = f"Ticket {t.id}: the builder reports ready, but no PR can be opened from {t.branch}: {why}. What should it do?"
        return _ask(replace(t, strikes=0), WaitReason.TROUBLE, question, f"no-pr:{_digest(why)}")
    text = f"The factory can't open a PR from {t.branch} into {t.base}: {why}. Commit, run `git push origin HEAD:{t.branch}`, and report ready."
    return Transition(replace(t, strikes=strikes, phase=Phase.BUILDING), inputs=(NewInput(InputKind.NOTE, text),))


def _reviewer_done(t: Ticket, assignment_id: str, pinned_sha: str | None, report: Report | None) -> Transition:
    """A verdict is posted to the forge, and only the forge's copy counts at gate 2."""
    if report is None or report.verdict is None:
        strikes = t.strikes + 1
        if strikes >= MAX_STRIKES:
            return _turn_trouble(replace(t, strikes=0), Role.REVIEWER, assignment_id, "the reviewer ended without a verdict")
        return Transition(replace(t, strikes=strikes, review_requested_sha=None), end_assignment=assignment_id)
    if t.pr is None or pinned_sha is None:
        return Transition(t, end_assignment=assignment_id)
    effect = EffectSpec(
        kind="post_review",
        key=f"review:{t.id}:{assignment_id}:{pinned_sha}",
        payload={
            "ticket": t.id,
            "repo": t.repo,
            "pr": t.pr,
            "sha": pinned_sha,
            "verdict": str(report.verdict),
            "findings": report.findings,
        },
    )
    return Transition(replace(t, strikes=0), effects=(effect,), end_assignment=assignment_id)


def worker_reply(t: Ticket, purpose: str, text: str, policy: Policy) -> Transition:
    """§6 reply: only on the Ticket's own Thread. The key comes from the text, so a recovery
    turn repeating itself posts nothing new."""
    if not text.strip() or t.stopped:
        return Transition(t)
    body = clamp_words(text, policy.chat_reply_words) if is_chat_thread(t.origin_thread) else text
    return Transition(t, effects=(comment(t, f"reply:{_digest(purpose + chr(0) + body)}", body),))


# --- the gates --------------------------------------------------------------------


def _engine_trigger(t: Ticket, thread: str, external_id: str, body: str, data: dict, now: float, actor: str = "factory") -> Trigger:
    return Trigger(
        source="engine",
        external_id=external_id,
        thread=thread,
        kind=TriggerKind.INTERNAL,
        actor=actor,
        actor_kind=ActorKind.SYSTEM,
        body=body,
        repo=t.repo,
        at=now,
        data=data,
    )


def gate_step(t: Ticket, facts: GateFacts, policy: Policy, now: float) -> Transition:
    """§5 steps 3-8, from one fresh read of the forge. Called only with host-read facts."""
    pr = facts.pr
    if t.pr_claim is not None and (
        pr is None or pr.number != t.pr_claim or pr.head_branch != t.branch or pr.base != t.base or pr.state != PrState.OPEN
    ):
        rejected = replace(t, pr_claim=None, strikes=t.strikes + 1, phase=Phase.BUILDING)
        if rejected.strikes >= MAX_STRIKES:
            question = f"Ticket {t.id}: the builder keeps claiming PR #{t.pr_claim}, which isn't an open PR from {t.branch} into {t.base}."
            return _ask(replace(rejected, strikes=0), WaitReason.TROUBLE, question, f"claim:{t.pr_claim}")
        text = f"PR #{t.pr_claim} isn't an open PR from {t.branch} into {t.base}. Check it and report again."
        return Transition(rejected, inputs=(NewInput(InputKind.NOTE, text),))
    if pr is None:
        return Transition(t)

    subscribe: tuple[tuple[str, SubMode], ...] = ()
    if t.pr_claim is not None:
        t = replace(t, pr=pr.number, last_pr=pr.number, pr_claim=None, head_sha=pr.head_sha)
        subscribe = ((pr_thread(t.repo, pr.number), SubMode.OWNER), (ci_thread(t.repo, pr.number), SubMode.OWNER))

    if pr.state in (PrState.MERGED, PrState.CLOSED):
        reason = EndReason.MERGED if pr.state == PrState.MERGED else EndReason.CLOSED
        ended = replace(t, status=TicketStatus.ENDED, end_reason=reason, pr=None, last_pr=pr.number, accepted_sha=None)
        return Transition(ended, subscribe=subscribe, drop_system_inputs=True)

    if t.phase not in (Phase.GATING, Phase.READY):
        return Transition(t, subscribe=subscribe)  # only the PR's fate matters while building or waiting

    result = _gates(replace(t, head_sha=pr.head_sha), facts, policy, now)
    return replace(result, subscribe=subscribe + result.subscribe)


def _gates(t: Ticket, facts: GateFacts, policy: Policy, now: float) -> Transition:
    sha = t.head_sha
    assert sha is not None and facts.ci is not None and facts.review is not None and facts.main is not None
    if t.accepted_sha is not None and t.accepted_sha != sha:
        t = replace(t, phase=Phase.GATING, accepted_sha=None)

    # Gate 1: CI on the exact head.
    if facts.ci.sha != sha or facts.ci.state == CiState.PENDING:
        return Transition(replace(t, phase=Phase.GATING, accepted_sha=None))
    if facts.ci.state == CiState.RED:
        return _ci_red(t, facts, now)

    # Gate 2: an independent review on that SHA. Only the reviewer bots' reviews count.
    review = facts.review
    if t.rereview:
        t = replace(t, rereview=False, review_floor=review.seq, review_requested_sha=None, passed_change=None)
    counts = (
        review.sha == sha
        and review.state != ReviewState.NONE
        and review.reviewer in policy.reviewer_bots
        and review.seq > t.review_floor
    )
    carried = not counts and review.state == ReviewState.NONE and _review_carries(t, facts, policy)
    if counts and review.state == ReviewState.PASS and facts.pr is not None and facts.pr.change:
        t = replace(t, passed_change=f"{sha} {facts.pr.change}")
    elif counts and review.state == ReviewState.CHANGES_REQUESTED:
        t = replace(t, passed_change=None)
    if not counts and not carried:
        if t.review_requested_sha == sha:
            return Transition(replace(t, phase=Phase.GATING, accepted_sha=None))
        if t.review_rounds >= policy.max_review_rounds:
            question = (
                f"Ticket {t.id}: {t.review_rounds} review rounds didn't reach a pass, and PR #{t.pr} is at {sha[:10]}. "
                "Tell me how to continue; your reply starts fresh rounds."
            )
            return _ask(t, WaitReason.ROUNDS, question, f"rounds:{sha}")
        return Transition(replace(t, phase=Phase.GATING, accepted_sha=None, review_requested_sha=sha), start_reviewer_sha=sha)
    if not carried and review.state == ReviewState.CHANGES_REQUESTED:
        return _changes_requested(t, facts, now)

    # Gate 3: the PR contains current main (§1.3).
    main = facts.main
    if not main.contains_main:
        token = f"{sha}:{main.main_sha}"
        moved = replace(t, phase=Phase.GATING, accepted_sha=None)
        if t.update_requested == token:
            return Transition(moved)
        update = EffectSpec(
            kind="update_branch",
            key=f"update:{t.id}:{token}",
            payload={"ticket": t.id, "repo": t.repo, "pr": t.pr, "sha": sha, "main_sha": main.main_sha},
        )
        return Transition(replace(moved, update_requested=token), effects=(update,))

    # All three on one SHA.
    if t.phase == Phase.READY and t.accepted_sha == sha:
        return Transition(t)
    text = f"Ticket {t.id}: PR #{t.pr} at {sha[:10]} passed CI and review and contains current {t.base}. Ready for you to merge."
    if carried:
        passed_on = (t.passed_change or "").split(" ")[0]
        text += f" The review passed on {passed_on[:10]}; since then the PR only took in {t.base}, and its diff against {t.base} is unchanged, so it carries."
    return Transition(replace(t, phase=Phase.READY, accepted_sha=sha, strikes=0), effects=(comment(t, f"ready:{sha}", text),))


def _review_carries(t: Ticket, facts: GateFacts, policy: Policy) -> bool:
    """Opt-in per repo: a pass carries to a new head when the PR's diff against main is the
    same change the reviewer passed. Merging main in cleanly keeps it; any real edit, or a conflict resolved, changes it and
    gets a fresh review. CI still has to pass on the new head (Gate 1), and main must still be contained (Gate 3)."""
    repo = policy.repo(t.repo)
    if repo is None or not repo.carry_review_across_main or not t.passed_change or facts.pr is None or not facts.pr.change:
        return False
    passed_sha, _, passed_change = t.passed_change.partition(" ")
    return passed_sha != t.head_sha and passed_change == facts.pr.change


def _ci_red(t: Ticket, facts: GateFacts, now: float) -> Transition:
    sha = t.head_sha
    assert sha is not None and t.pr is not None and facts.ci is not None
    if t.ci_red_sha == sha:
        # The builder saw this SHA's failure and reported ready again without a new commit.
        question = f"Ticket {t.id}: the builder says it's ready, but CI is still red on {sha[:10]}. What should it do?"
        return _ask(replace(t, accepted_sha=None), WaitReason.TROUBLE, question, f"still-red:{sha}")
    log = "\n\n".join(f"{f.context}:\n{f.log_tail}" for f in facts.ci.failures) or "No log available."
    trigger = _engine_trigger(t, ci_thread(t.repo, t.pr), f"ci-red:{t.id}:{sha}", log, {"sha": sha}, now, actor="ci")
    text = f"CI is red on your head {sha[:10]}, PR #{t.pr}.\n\n" + cards.log_card(log, author="ci", kind="ci_log", ref=trigger.id)
    woken = replace(t, phase=Phase.BUILDING, accepted_sha=None, ci_red_sha=sha)
    return Transition(woken, triggers=(trigger,), inputs=(NewInput(InputKind.TRIGGER, text, ref=trigger.id),))


def _changes_requested(t: Ticket, facts: GateFacts, now: float) -> Transition:
    sha = t.head_sha
    assert sha is not None and t.pr is not None and facts.review is not None
    if t.changes_sha == sha:
        question = f"Ticket {t.id}: the builder says it's ready, but {sha[:10]} is the commit the reviewer asked changes on. Who is right?"
        return _ask(replace(t, accepted_sha=None), WaitReason.TROUBLE, question, f"same-sha:{sha}")
    review = facts.review
    trigger = _engine_trigger(
        t, pr_thread(t.repo, t.pr), f"review:{t.id}:{sha}", review.findings, {"sha": sha}, now, actor=review.reviewer
    )
    text = f"The reviewer asked for changes on {sha[:10]}, PR #{t.pr}.\n\n" + cards.spec_card(
        review.findings, author=review.reviewer, kind="review", ref=trigger.id, neighbours=(t.id,)
    )
    woken = replace(t, phase=Phase.BUILDING, accepted_sha=None, changes_sha=sha, review_rounds=t.review_rounds + 1)
    return Transition(woken, triggers=(trigger,), inputs=(NewInput(InputKind.TRIGGER, text, ref=trigger.id),))


def update_conflicted(t: Ticket) -> Transition:
    """§5.6: a conflict wakes the builder to merge main itself."""
    if t.stopped or not t.is_open:
        return Transition(t)
    text = f"{t.base} moved and updating your branch conflicts. Merge {t.base} into {t.branch}, push, and report ready."
    return Transition(replace(t, phase=Phase.BUILDING, accepted_sha=None), inputs=(NewInput(InputKind.NOTE, text),))


# --- budgets ----------------------------------------------------------------------


def budget_question(t: Ticket) -> Transition:
    """§1.2, §7: reaching a budget asks the owner and keeps everything: session, worktree, queue."""
    question = (
        f"Ticket {t.id} has used {t.tokens} of {t.budget_tokens} tokens and {int(t.wall_seconds)} of "
        f"{t.budget_wall_seconds} working seconds, and has more to do. Reply to continue with another budget, or `stop`."
    )
    return _ask(t, WaitReason.BUDGET, question, f"budget:{t.budget_tokens}:{t.budget_wall_seconds}")


def may_start_turn(t: Ticket, role: Role, now: float) -> bool:
    if t.held or t.quarantined or t.stopped or not t.is_open or now < t.next_turn_at:
        return False
    return role == Role.REVIEWER or t.phase != Phase.WAITING
