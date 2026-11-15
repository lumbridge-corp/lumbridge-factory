"""The domain engine: the one writer.

Each step reads what it needs, asks the pure core what should happen, and stores all of it in
one transaction. Outside effects run later from the Outbox. `tick()` is one pass; factoryd calls
it in a loop.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import replace
from typing import Any, Callable, Sequence

from .core import capacity, cards, prompts, tickets
from .core.executor import (
    EntryKind,
    EventType,
    Executor,
    ExecutorError,
    InboxEntry,
    InputKind,
    PromptParts,
    Report,
    Role,
    StartRequest,
    TurnEvent,
    TurnInput,
    TurnOutcome,
    TurnRequest,
    TurnResult,
    WorktreeSpec,
    entry_from_wire,
    report_from_wire,
    start_request_from_wire,
    to_wire,
    turn_request_from_wire,
)
from .core.intake import GateResult, Route, gate
from .core.model import (
    Assignment,
    Decision,
    Mandate,
    PERSON_KINDS,
    WaitReason,
    Phase,
    RouterDecision,
    RuleMode,
    Strength,
    SubMode,
    Ticket,
    Trigger,
    TriggerKind,
    clamp_words,
    ticket_id as derive_ticket_id,
)
from .core.policy import Policy, PolicyError, RepoPolicy
from .effects import EffectRow, Outbox, UnknownOutcome
from .gates import Gates, read_facts
from .ports import AccountRouter, Assessment, Assessor, CannotCheck, Channel, Source
from .store import Store
from .turns import InlineRunner, Runner, StreamBroken

ASSESS_RETRY_SECONDS = 300.0
CLEANUP_EVERY_SECONDS = 60.0
TERMINAL_TURN = ("done", "cancelled")
# A job can only have been killed by an outage if the machine came up recently.
BOOT_RERUN_WINDOW_SECONDS = 6 * 3600.0

log = logging.getLogger("lumbridge_factory.engine")


def _recall_text(trigger: Trigger) -> str:
    """A `factory` label carries no text of its own: the issue it was put on is what recall should read."""
    issue = str(trigger.data.get("issue_body") or "")
    if trigger.body or not issue:
        return trigger.body
    title = str(trigger.data.get("title") or "")
    return f"{title}\n\n{issue}" if title else issue


def _recall_author(trigger: Trigger) -> str:
    if not trigger.body and trigger.data.get("issue_body"):
        return str(trigger.data.get("issue_author") or trigger.actor)
    return trigger.actor


class Engine:
    def __init__(
        self,
        store: Store,
        *,
        executor: Executor | None = None,
        gates: Gates | None = None,
        channel: Channel | None = None,
        accounts: AccountRouter | None = None,
        assessor: Assessor | None = None,
        runner: Runner | None = None,
        sources: Sequence[Source] = (),
        clock: Callable[[], float] = time.time,
        spool_dir: str | None = None,
        source_poll_seconds: float = 0.0,  # §3: Sources and gate reads hit the forge, so factoryd
        gate_read_seconds: float = 0.0,  # spaces them out. Tests read on every tick.
        retain_ended_seconds: float | None = None,  # None: this engine cleans nothing up
        boot_time: float | None = None,  # for §8's CI after a reboot; None: never rerun
    ):
        self.store = store
        self.executor = executor
        self.gates = gates
        self.channel = channel
        self.accounts = accounts
        self.assessor = assessor
        self.runner: Runner = runner or InlineRunner()
        self.sources = tuple(sources)
        self.now = clock
        self.outbox = Outbox(store, clock)
        self.spool_dir = spool_dir
        self.source_poll_seconds = source_poll_seconds
        self.gate_read_seconds = gate_read_seconds
        self.retain_ended_seconds = retain_ended_seconds
        self.boot_time = boot_time
        self._source_next: dict[str, float] = {}
        self._gate_next: dict[str, float] = {}
        self._cleanup_next = 0.0
        self._needs_recovery = True
        self._handlers = {
            "comment": self._do_comment,
            "post_review": self._do_post_review,
            "update_branch": self._do_update_branch,
            "start_turn": self._do_start_turn,
            "stop_session": self._do_stop_session,
            "forget_turn": self._do_forget_turn,
            "answer_recall": self._do_answer_recall,
            "assess": self._do_assess,
            "ensure_pr": self._do_ensure_pr,
            "remove_session": self._do_remove_session,
            "ci_rerun": self._do_ci_rerun,
        }
        self._completions = {
            "start_turn": self._after_start_turn,
            "update_branch": self._after_update_branch,
            "assess": self._after_assess,
            "ensure_pr": self._after_ensure_pr,
            "remove_session": self._after_remove_session,
        }

    # --- policy -------------------------------------------------------------------

    def bootstrap(self, policy: Policy, actor: str = "install") -> None:
        """Install the first policy. Refuses one that leaves anything undecided (§2)."""
        policy.validate()
        if self.store.policy() is None:
            with self.store.tx():
                self.store.add_policy(replace(policy, version=1), actor, self.now())

    @property
    def policy(self) -> Policy:
        policy = self.store.policy()
        if policy is None:
            raise RuntimeError("no policy installed")
        return policy

    def set_policy(self, policy: Policy, actor: str) -> Policy:
        """A new version. Open Tickets keep their Mandates; new decisions use the new one."""
        current = self.policy
        policy.validate()
        versioned = replace(policy, version=current.version + 1)
        with self.store.tx():
            self.store.add_policy(versioned, actor, self.now())
        return versioned

    # --- the loop -----------------------------------------------------------------

    def tick(self) -> int:
        """One pass. Returns how many things moved; 0 means nothing to do right now."""
        moved = 0
        if self._needs_recovery:
            moved += self.recover()
        moved += self.drain_spool()
        moved += self.poll_sources()
        moved += self.evaluate_threads()
        moved += self.drain_turns()
        moved += self.advance_gates()
        moved += self.schedule()
        moved += self.clean_up()
        moved += self.pump()
        return moved

    def settle(self, limit: int = 200) -> None:
        """Tick until nothing moves. For tests and the CLI."""
        for _ in range(limit):
            if self.tick() == 0:
                return
        raise RuntimeError("the engine did not settle")

    def pump(self) -> int:
        return self.outbox.pump(self._handlers, self._completions, self._effect_uncertain)

    # --- intake -------------------------------------------------------------------

    def ingest(self, trigger: Trigger) -> bool:
        """Store a Trigger once and evaluate its Thread. False if it was already seen."""
        with self.store.tx():
            if not self.store.add_trigger(trigger, self.now()):
                return False
            self._evaluate_thread(trigger.thread)
        return True

    def poll_sources(self) -> int:
        moved, now = 0, self.now()
        for source in self.sources:
            if now < self._source_next.get(source.name, 0.0):
                continue
            self._source_next[source.name] = now + self.source_poll_seconds
            try:
                events, cursor = source.poll(self.store.cursor(source.name))
            except Exception as exc:
                log.warning("source %s: %s", source.name, exc)
                continue  # a Source being down is ordinary; the cursor hasn't moved
            with self.store.tx():
                for event in events:
                    moved += int(self.store.add_trigger(event, self.now()))
                # The cursor moves with the Triggers: a restart never replays an event.
                self.store.set_cursor(source.name, cursor, self.now())
        return moved

    def evaluate_threads(self) -> int:
        moved = 0
        for thread in self.store.pending_threads():
            with self.store.tx():
                moved += self._evaluate_thread(thread)
        return moved

    def _evaluate_thread(self, thread: str) -> int:
        """§4: at most one Eval on a Thread; what arrives meanwhile batches into the next."""
        if self.store.running_eval(thread):
            return 0
        policy, now, moved = self.policy, self.now(), 0
        batch: list[Trigger] = []
        batch_rule = ""
        for trigger, hint in self.store.pending_triggers(thread):
            subs = self.store.subs_of_thread(thread)
            result = GateResult(Route.CANDIDATE, rule="handback") if hint == "handback" else gate(trigger, policy, subs)
            if result.route == Route.CANDIDATE:
                if policy.rules.get(result.rule, RuleMode.OFF) == RuleMode.OFF:
                    self.store.route_trigger(trigger.id, "ignored", f"rule {result.rule} is off")
                    moved += 1
                elif now >= self.store.next_assess_at(thread) and batch_rule in ("", result.rule):
                    batch_rule = result.rule
                    batch.append(trigger)
                continue  # otherwise it stays pending for a later Eval
            self._route(trigger, result)
            moved += 1
        if batch:
            self._open_assessment(thread, batch_rule, batch)
            moved += 1
        return moved

    def _route(self, trigger: Trigger, result: GateResult) -> None:
        route = result.route
        if route == Route.NOT_ADMITTED:
            self.store.route_trigger(trigger.id, "not_admitted", result.reason)
        elif route == Route.IGNORE:
            self.store.route_trigger(trigger.id, "ignored", result.reason)
        elif route == Route.FACT:
            self.store.route_trigger(trigger.id, "fact")
            self._wake_gates(trigger.thread)
        elif route == Route.ACCEPT:
            self._accept(trigger, result.rule, Strength.EXPLICIT, trigger.actor)
        elif route == Route.FOLLOW_UP:
            self._deliver_follow_up(trigger)
        elif route == Route.STOP:
            self._stop_from(trigger)
        elif route == Route.COMMAND:
            self._command_from(trigger)

    def _wake_gates(self, thread: str) -> None:
        """The forge told us something changed: don't wait for the next spaced-out read."""
        for sub in self.store.subs_of_thread(thread):
            self._gate_next.pop(sub.ticket_id, None)

    def _deliver_follow_up(self, trigger: Trigger) -> None:
        delivered = None
        for sub in self.store.subs_of_thread(trigger.thread):
            ticket = self.store.ticket(sub.ticket_id)
            if ticket is None or ticket.stopped:
                continue
            step = tickets.follow_up if sub.mode == SubMode.OWNER else tickets.context_only
            self._apply(step(ticket, trigger))
            delivered = ticket.id
        self.store.route_trigger(trigger.id, "delivered", ticket_id=delivered)

    def _stop_from(self, trigger: Trigger) -> None:
        named = trigger.data.get("ticket") if trigger.kind == TriggerKind.COMMAND else None
        if named:
            ids = [named]
        else:
            ids = [s.ticket_id for s in self.store.subs_of_thread(trigger.thread) if s.mode == SubMode.OWNER and not s.stopped]
        for id in ids:
            ticket = self.store.ticket(id)
            if ticket is not None:
                self._apply(tickets.stop(ticket, trigger.actor))
        self.store.route_trigger(trigger.id, "delivered", ticket_id=ids[0] if ids else None)

    def _command_from(self, trigger: Trigger) -> None:
        command, ticket = trigger.data["command"], self.store.ticket(trigger.data.get("ticket", ""))
        if ticket is None:
            self.store.route_trigger(trigger.id, "ignored", "no such ticket")
            return
        step = {"hold": tickets.hold, "release": tickets.release, "retry": tickets.retry}[command]
        if command == "retry":
            self.outbox.retry_ticket(ticket.id)
        self._apply(step(ticket))
        self.store.route_trigger(trigger.id, "delivered", ticket_id=ticket.id)

    def command(self, command: str, ticket: str, actor: str) -> None:
        """stop, hold, release or retry, from the local CLI. Recorded like any other Trigger."""
        t = self.store.ticket(ticket)
        if t is None:
            raise KeyError(ticket)
        self.ingest(
            Trigger(
                source="cli",
                external_id=str(uuid.uuid4()),
                thread=t.origin_thread,
                kind=TriggerKind.COMMAND,
                actor=actor,
                body=command,
                repo=t.repo,
                at=self.now(),
                data={"command": command, "ticket": ticket},
            )
        )

    # --- accept -------------------------------------------------------------------

    def _accept(self, trigger: Trigger, rule: str, strength: Strength, requested_by: str, note: str = "") -> Ticket:
        policy, now = self.policy, self.now()
        repo = policy.repo(trigger.repo)
        assert repo is not None, "the gate admitted a repository the policy doesn't have"
        id = derive_ticket_id(trigger)
        mandate = Mandate(
            id="m-" + id[2:],
            ticket_id=id,
            policy_version=policy.version,
            repo=repo.name,
            base=repo.base,
            origin_thread=trigger.thread,
            strength=strength,
            rules=(rule,),
            requested_by=requested_by,
            trigger_id=trigger.id,
            budget_tokens=policy.budget_tokens,
            budget_wall_seconds=policy.budget_wall_seconds,
            created_at=now,
        )
        ticket = tickets.new_ticket(mandate, str(trigger.data.get("title") or ""))
        self.store.save_ticket(ticket, now)  # before the Mandate's Eval and the first Assignment
        self.store.add_mandate(mandate)
        self.store.add_assignment(self._new_builder(ticket))
        if strength == Strength.EXPLICIT:  # an inferred accept finishes the Eval already open
            eval_id = "ev-" + hashlib.sha256(trigger.id.encode()).hexdigest()[:10]
            self.store.add_eval(
                eval_id, trigger.thread, "done", rule, RuleMode.AUTO, strength, [trigger.id], now,
                decision=Decision.ACCEPT, ticket_id=id,
            )
        # A Thread keeps one owner: the owner's next word there belongs to the Ticket that already
        # has it. A Ticket born from a hand-back follows the Thread and owns its own PR.
        taken = any(s.mode == SubMode.OWNER and not s.stopped for s in self.store.subs_of_thread(trigger.thread))
        self._apply(tickets.accepted(ticket, trigger, rule, note, SubMode.CONTEXT if taken else SubMode.OWNER))
        self.store.route_trigger(trigger.id, "accepted", ticket_id=id)
        return ticket

    def _new_builder(self, t: Ticket) -> Assignment:
        n = self.store.count_assignments(t.id, Role.BUILDER) + 1
        return Assignment(
            id=f"{t.id}-b{n}",
            ticket_id=t.id,
            role=Role.BUILDER,
            worktree=WorktreeSpec(repo=t.repo, name=t.id, branch=t.branch, base=t.base),
            model=self.policy.builder_model,
            created_at=self.now(),
        )

    # --- applying a transition -----------------------------------------------------

    def _apply(self, tr: tickets.Transition, turn_id: str | None = None) -> None:
        """Inside a transaction: everything a Transition names, or nothing."""
        t, now, store = tr.ticket, self.now(), self.store
        store.save_ticket(t, now)
        for thread, mode in tr.subscribe:
            store.subscribe(t.id, thread, mode)
        for thread in tr.unsubscribe:
            store.unsubscribe(t.id, thread)
        for trigger in tr.triggers:
            store.add_trigger(trigger, now, status="delivered", ticket_id=t.id)
        if tr.requeue_inputs and turn_id:
            store.requeue_inputs(turn_id)
        if tr.replace_builder:
            self._replace_builder(t)
        builder = store.live_builder(t.id)
        for item in tr.inputs:
            assert builder is not None, "a Ticket always has a builder"
            if item.kind == InputKind.RECOVERY:
                store.drop_recovery_notes(builder.id)
            store.add_input(builder.id, t.id, item.kind, item.text, item.ref, item.wake, item.urgent, now)
        for effect in tr.effects:
            self.outbox.enqueue(effect.kind, effect.key, effect.payload, t.id)
        if tr.start_reviewer_sha:
            self._start_reviewer(t, tr.start_reviewer_sha)
        if tr.end_assignment:
            store.update_assignment(tr.end_assignment, live=0)
        if tr.stop_work:
            self._stop_work(t)
        if tr.drop_system_inputs:
            store.drop_pending_inputs(t.id, system_only=True)

    def _replace_builder(self, t: Ticket) -> None:
        """A session that can't resume: new Assignment, same worktree, the original assignment
        again, plus whatever hadn't been read (§4 Assignment)."""
        old = self.store.live_builder(t.id)
        assert old is not None
        new = self._new_builder(t)
        self.store.update_assignment(old.id, live=0)
        self.store.add_assignment(new)
        self.store.move_pending_inputs(old.id, new.id)
        self.store.drop_recovery_notes(new.id)
        first = self.store.first_assignment_input(old.id)
        if first is not None and first["turn_id"] is not None:
            self.store.add_input(new.id, t.id, first["kind"], first["text"], first["ref"], True, bool(first["urgent"]), self.now())
        self.store.add_input(new.id, t.id, InputKind.RECOVERY, prompts.RECOVERY_NOTE, "", True, False, self.now())

    def _start_reviewer(self, t: Ticket, sha: str) -> None:
        self._retire_reviewers(t)
        n = self.store.count_assignments(t.id, Role.REVIEWER) + 1
        assignment = Assignment(
            id=f"{t.id}-r{n}",
            ticket_id=t.id,
            role=Role.REVIEWER,
            worktree=WorktreeSpec(repo=t.repo, name=f"{t.id}-r{n}", branch="", base=t.base, pinned_sha=sha, read_only=True),
            model=self.policy.reviewer_model,
            created_at=self.now(),
        )
        self.store.add_assignment(assignment)
        text = f"Review PR #{t.pr} on {t.repo} at exactly {sha}, against {t.base}. This worktree is that commit. The request:"
        mandate = self.store.mandate(t.mandate_id)
        request = self.store.trigger(mandate.trigger_id) if mandate else None
        if request is not None:
            text += "\n\n" + tickets.assignment_text(t, request)
        later = self._later_requests(t)
        if later:
            text += "\n\nLater requests on this Ticket, from people allowed to ask. They are part of its scope:\n\n" + "\n\n".join(later)
        said = self._builder_said(t)
        if said:
            text += "\n\nThe builder's last report, including what it says it ran. A claim, not a fact:\n\n" + said
        self.store.add_input(assignment.id, t.id, InputKind.ASSIGNMENT, text, "", True, False, self.now())

    def _builder_said(self, t: Ticket) -> str:
        """Found on a real PR: the builder said it hadn't run the test the issue named, and the reviewer never saw that."""
        builder = self.store.live_builder(t.id)
        summary = ""
        for row in self.store.entries(ticket_id=t.id) if builder else ():
            turn = self.store.turn(row["turn_id"])
            report = row["body"].get("report") if row["kind"] == EntryKind.REPORT else None
            if report and turn and turn["assignment_id"] == builder.id and report.get("summary"):
                summary = report["summary"]
        return cards.fence(summary[:4000], author="builder", kind="report", ref=builder.id) if summary else ""

    def _later_requests(self, t: Ticket) -> list[str]:
        """What the gate let through after the first request. A reviewer that only knows the first
        one fails the builder for doing what the owner asked (measured on the sandbox, PR #7)."""
        out = []
        for ref in self.store.delivered_refs(t.id):
            trigger = self.store.trigger(ref)
            routing = self.store.routing(ref)
            if trigger is not None and routing is not None and trigger.kind in PERSON_KINDS and routing["status"] == "delivered":
                out.append(cards.spec_card(trigger.body, author=trigger.actor, kind=str(trigger.kind), ref=trigger.id))
        return out

    def _stop_turns(self, t: Ticket, role: Role | None = None) -> None:
        for turn in self.store.turns(ticket_id=t.id, statuses=("launching", "running")):
            assignment = self.store.assignment(turn["assignment_id"])
            if assignment is None or (role is not None and assignment.role != role):
                continue
            if turn["status"] == "launching":
                self.outbox.cancel(f"start_turn:{turn['id']}")
                self.store.requeue_inputs(turn["id"])
                self.store.update_turn(turn["id"], status="cancelled", outcome=TurnOutcome.STOPPED, ended_at=self.now())
            if assignment.session_id:
                self.outbox.enqueue("stop_session", f"stop:{turn['id']}", {"session_id": assignment.session_id}, t.id)

    def _retire_reviewers(self, t: Ticket) -> None:
        """A review of a commit that is no longer the head is wasted: its slot comes back."""
        self._stop_turns(t, Role.REVIEWER)
        for assignment in self.store.assignments(t.id, live_only=True):
            if assignment.role == Role.REVIEWER:
                self.store.update_assignment(assignment.id, live=0)

    def _stop_work(self, t: Ticket) -> None:
        self._stop_turns(t)
        self.store.drop_pending_inputs(t.id)
        self._retire_reviewers(t)

    # --- the Router ----------------------------------------------------------------

    def _open_assessment(self, thread: str, rule: str, batch: list[Trigger]) -> None:
        policy, now = self.policy, self.now()
        ids = [t.id for t in batch]
        eval_id = "ev-" + hashlib.sha256("\0".join(ids).encode() + str(self.store.count_evals()).encode()).hexdigest()[:10]
        self.store.add_eval(eval_id, thread, "running", rule, policy.rules[rule], Strength.INFERRED, ids, now)
        for trigger in batch:
            self.store.route_trigger(trigger.id, "assessing", eval_id=eval_id)
        payload = {
            "eval_id": eval_id,
            "rule": rule,
            "thread": thread,
            # Verbatim and fenced. The Router forwards Triggers; it never gets to rewrite them.
            "cards": [cards.card(t.body, author=t.actor, kind=str(t.kind), ref=t.id) for t in batch],
        }
        self.outbox.enqueue("assess", f"assess:{eval_id}", payload)

    def _do_assess(self, row: EffectRow) -> dict:
        p = row.payload
        if self.assessor is None:
            return {"decision": Decision.UNAVAILABLE}
        try:
            d = self.assessor.assess(Assessment(p["eval_id"], p["rule"], p["thread"], tuple(p["cards"])))
        except Exception as exc:  # the Router being down is ordinary: try again later
            return {"decision": Decision.UNAVAILABLE, "note": str(exc)[:200]}
        return {
            "decision": d.decision,
            "would_accept": d.would_accept,
            "ticket_id": d.ticket_id,
            "note": clamp_words(d.note, 30),
            "title": d.title,
        }

    def _after_assess(self, row: EffectRow, result: dict) -> None:
        self._assessed(row.payload["eval_id"], RouterDecision(
            decision=Decision(result["decision"]),
            would_accept=bool(result.get("would_accept")),
            ticket_id=result.get("ticket_id"),
            note=result.get("note") or "",
            title=result.get("title") or "",
        ))

    def _assessed(self, eval_id: str, d: RouterDecision) -> None:
        ev = self.store.eval(eval_id)
        assert ev is not None
        now, thread = self.now(), ev["thread"]
        batch = [self.store.trigger(i) for i in ev["trigger_ids"]]
        would_accept = d.decision == Decision.ACCEPT or d.would_accept
        rule = ev["rule"]

        if d.decision == Decision.UNAVAILABLE:
            self.store.finish_eval(eval_id, Decision.UNAVAILABLE, False, d.note, None, now)
            for trigger in batch:
                self.store.repend_trigger(trigger.id, "handback" if rule == "handback" else "")
            self.store.set_next_assess_at(thread, now + ASSESS_RETRY_SECONDS)
            return

        if ev["mode"] == RuleMode.OBSERVE:
            self.store.finish_eval(eval_id, Decision.IGNORE, would_accept, d.note, None, now)
            for trigger in batch:
                self.store.route_trigger(trigger.id, "observed", eval_id=eval_id)
            self._evaluate_thread(thread)
            return

        ticket_id = None
        if d.decision == Decision.ACCEPT:
            first = batch[0]
            requested_by = first.actor if first.kind in PERSON_KINDS else f"rule:{rule}"
            rule_name = "build" if rule == "handback" else rule
            ticket_id = self._accept(first, rule_name, Strength.INFERRED, requested_by, d.note).id
            for trigger in batch[1:]:
                self.store.route_trigger(trigger.id, "delivered", eval_id=eval_id, ticket_id=ticket_id)
        elif d.decision == Decision.ATTACH and d.ticket_id and self._can_attach(d.ticket_id, batch):
            ticket_id = d.ticket_id
            target = self.store.ticket(ticket_id)
            self.store.subscribe(ticket_id, thread, SubMode.CONTEXT)
            for trigger in batch:
                self._apply(tickets.follow_up(target, trigger))
                target = self.store.ticket(ticket_id)
                self.store.route_trigger(trigger.id, "delivered", eval_id=eval_id, ticket_id=ticket_id)
        elif d.decision == Decision.STOP and d.ticket_id and all(t.kind in PERSON_KINDS for t in batch):
            target = self.store.ticket(d.ticket_id)
            if target is not None and any(s.ticket_id == target.id for s in self.store.subs_of_thread(thread)):
                self._apply(tickets.stop(target, batch[0].actor))
            for trigger in batch:
                self.store.route_trigger(trigger.id, "delivered", eval_id=eval_id, ticket_id=d.ticket_id)
        else:
            for trigger in batch:
                self.store.route_trigger(trigger.id, "ignored", d.note, eval_id=eval_id)
            if rule == "handback":
                key = f"comment:handback:{eval_id}"
                text = "The builder handed this back as outside its ticket, and it didn't become new work. Tell me if it should."
                self.outbox.enqueue("comment", key, {"thread": thread, "text": text})
        self.store.finish_eval(eval_id, d.decision, would_accept, d.note, ticket_id, now)
        self._evaluate_thread(thread)

    def _can_attach(self, ticket_id: str, batch: list[Trigger]) -> bool:
        target = self.store.ticket(ticket_id)
        if target is None or target.stopped:
            return False
        # A Ticket that handed something back doesn't get it again.
        return not any(self.store.delivered_ref(ticket_id, t.id) for t in batch)

    # --- scheduling ----------------------------------------------------------------

    def schedule(self) -> int:
        """§7: a turn takes a slot only while it runs. Tickets waiting on CI, review or the owner
        aren't candidates at all."""
        policy, now, launched = self.policy, self.now(), 0
        with self.store.tx():
            ready: list[capacity.Candidate] = []
            for row in self.store.candidates():
                ticket = self.store.ticket(row["ticket_id"])
                role = Role(row["role"])
                if ticket is None or not tickets.may_start_turn(ticket, role, now):
                    continue
                urgent = bool(row["urgent"])
                if role == Role.BUILDER and not urgent and ticket.over_budget:
                    self._apply(tickets.budget_question(ticket))
                    launched += 1
                    continue
                ready.append(
                    capacity.Candidate(row["assignment_id"], row["ticket_id"], role, urgent, bool(row["native_session_id"]), row["position"])
                )
            used = self.store.slots_used()
            chosen = capacity.pick(
                ready, used.get(capacity.GENERAL, 0), used.get(capacity.RESERVED, 0), self.effective_slots(policy), policy.reserved_slots
            )
            for candidate, slot in chosen:
                launched += int(self._launch(candidate, slot))
        return launched

    def effective_slots(self, policy: Policy) -> int:
        """`slots`, or `tight_slots` once the tight account has used `tight_pct` of its allowance, as the accounts provider
        reports it (`used_pct`). Not knowing counts as tight."""
        if not policy.tight_account:
            return policy.slots
        reader = getattr(self.accounts, "used_pct", None)
        used = reader(policy.tight_account) if reader else None
        return policy.slots if used is not None and used < policy.tight_pct else min(policy.slots, policy.tight_slots)

    def _launch(self, c: capacity.Candidate, slot: str) -> bool:
        policy, now = self.policy, self.now()
        assignment = self.store.assignment(c.assignment_id)
        ticket = self.store.ticket(c.ticket_id)
        assert assignment is not None and ticket is not None
        avoid = ""
        if c.role == Role.REVIEWER:
            builder = self.store.live_builder(ticket.id)
            avoid = builder.account if builder else ""
        account = self.accounts.pick(c.role, avoid) if self.accounts else "default"
        if account is None:
            return False  # every account is at its limit: the turn waits, nothing fails

        turn_id = f"{assignment.id}-{self.store.count_turns(assignment.id) + 1}"
        rows = self.store.pending_inputs(assignment.id)
        inputs = tuple(TurnInput(InputKind(r["kind"]), r["text"], r["ref"]) for r in rows)
        self.store.bind_inputs([r["id"] for r in rows], turn_id)
        request = TurnRequest(
            turn_id=turn_id,
            session_id=assignment.session_id or "",
            native_session_id=assignment.native_session_id,
            inputs=inputs,
            account=account,
            model=assignment.model,
            limit_seconds=policy.turn_limit_seconds,
        )
        self.store.add_turn(turn_id, ticket.id, assignment.id, slot, to_wire(request), now)
        self.store.update_assignment(assignment.id, account=account)
        payload: dict[str, Any] = {"turn": to_wire(request), "assignment": assignment.id}
        if assignment.session_id is None:
            payload["start"] = to_wire(self._start_request(ticket, assignment, account))
        self.outbox.enqueue("start_turn", f"start_turn:{turn_id}", payload, ticket.id)
        if c.role == Role.BUILDER and ticket.phase != Phase.BUILDING:
            self.store.save_ticket(replace(ticket, phase=Phase.BUILDING, accepted_sha=None), now)
        return True

    def _start_request(self, t: Ticket, a: Assignment, account: str) -> StartRequest:
        mandate = self.store.mandate(t.mandate_id)
        rule = mandate.rules[0] if mandate and mandate.rules else "build"
        rules = prompts.instance_rules(self.policy, t.repo)
        if a.role == Role.REVIEWER:
            parts = PromptParts(system=prompts.REVIEWER_SYSTEM, rule_instructions=f"{prompts.OUTBOUND_GUARD}\n\n{rules}")
        else:
            parts = PromptParts(system=prompts.BUILDER_SYSTEM, rule_instructions=f"{tickets.rule_prompt(rule)}\n\n{rules}")
        return StartRequest(t.id, a.id, a.role, a.worktree, parts, account, a.model)

    # --- effect handlers ------------------------------------------------------------

    def _do_comment(self, row: EffectRow) -> None:
        p = row.payload
        assert self.channel is not None
        try:
            if self.channel.find_comment(p["thread"], row.key):
                return
        except CannotCheck as exc:
            raise UnknownOutcome(f"a comment on {p['thread']} ({exc})") from exc
        self.channel.post_comment(p["thread"], p["text"], row.key)

    def _do_post_review(self, row: EffectRow) -> None:
        p = row.payload
        assert self.channel is not None
        try:
            if self.channel.find_review(p["repo"], p["pr"], row.key):
                return
        except CannotCheck as exc:
            raise UnknownOutcome(f"a review on PR #{p['pr']} ({exc})") from exc
        self.channel.post_review(p["repo"], p["pr"], p["sha"], p["verdict"], p["findings"], row.key)

    def _do_ensure_pr(self, row: EffectRow) -> dict:
        assert self.channel is not None
        p = row.payload
        return self.channel.ensure_pr(p["repo"], p["branch"], p["base"], p["title"], p["body"], row.key)

    def _after_ensure_pr(self, row: EffectRow, result: dict) -> None:
        ticket = self.store.ticket(row.payload["ticket"])
        if ticket is None:
            return
        self._gate_next.pop(ticket.id, None)
        if "pr" in result:
            self._apply(tickets.pr_opened(ticket, int(result["pr"])))
        else:
            self._apply(tickets.pr_refused(ticket, str(result.get("refused") or "the forge refused")))

    def _do_remove_session(self, row: EffectRow) -> None:
        assert self.executor is not None
        self.executor.remove_session(row.payload["session_id"], bool(row.payload["worktree"]))

    def _after_remove_session(self, row: EffectRow, _: Any) -> None:
        if not row.payload["worktree"] or row.payload["role"] != Role.BUILDER:
            return
        ticket = self.store.ticket(row.ticket_id) if row.ticket_id else None
        if ticket is not None and not ticket.is_open:
            self.store.save_ticket(replace(ticket, worktree_gone=True), self.now())

    def _do_ci_rerun(self, row: EffectRow) -> None:
        assert self.channel is not None
        self.channel.rerun_job(row.payload["repo"], row.payload["run_id"], row.payload["job_id"])

    def _do_update_branch(self, row: EffectRow) -> str:
        assert self.channel is not None
        return self.channel.update_branch(row.payload["repo"], row.payload["pr"])

    def _after_update_branch(self, row: EffectRow, outcome: str) -> None:
        if outcome == "conflict":
            ticket = self.store.ticket(row.payload["ticket"])
            if ticket is not None:
                self._apply(tickets.update_conflicted(ticket))

    def _do_start_turn(self, row: EffectRow) -> dict:
        assert self.executor is not None
        p = row.payload
        assignment = self.store.assignment(p["assignment"])
        assert assignment is not None
        request = turn_request_from_wire(p["turn"])
        try:
            session_id = assignment.session_id
            if session_id is None:
                session_id = self.executor.start(start_request_from_wire(p["start"]))
            self.runner.launch(self.executor, replace(request, session_id=session_id))
        except ExecutorError as exc:
            if exc.retryable:
                raise  # the Outbox backs off and tries again
            return {"failed": str(exc)}
        return {"session_id": session_id}

    def _after_start_turn(self, row: EffectRow, result: dict) -> None:
        turn_id, session_id = row.payload["turn"]["turn_id"], result.get("session_id") or ""
        if "failed" in result:
            self._absorb_result(TurnResult(turn_id, session_id, TurnOutcome.FAILED, error=result["failed"]))
            return
        self.store.update_assignment(row.payload["assignment"], session_id=session_id)
        turn = self.store.turn(turn_id)
        if turn is not None and turn["status"] == "launching":
            self.store.update_turn(turn_id, status="running", last_event_at=self.now())

    def _do_stop_session(self, row: EffectRow) -> None:
        assert self.executor is not None
        self.executor.stop(row.payload["session_id"])

    def _do_forget_turn(self, row: EffectRow) -> None:
        assert self.executor is not None
        self.executor.forget(row.payload["turn_id"])

    def _do_answer_recall(self, row: EffectRow) -> None:
        assert self.executor is not None
        p = row.payload
        self.executor.answer_recall(p["session_id"], p["request_id"], p["text"])

    def _effect_uncertain(self, row: EffectRow, what: str) -> None:
        ticket = self.store.ticket(row.ticket_id) if row.ticket_id else None
        if ticket is not None:
            self._apply(tickets.quarantine(ticket, what))

    # --- turn results ---------------------------------------------------------------

    def drain_turns(self) -> int:
        moved = 0
        for item in self.runner.drain():
            moved += 1
            with self.store.tx():
                if isinstance(item, TurnResult):
                    self._absorb_result(item)
                elif isinstance(item, TurnEvent):
                    self._absorb_event(item)
                elif isinstance(item, StreamBroken):
                    if item.retryable:
                        self._needs_recovery = True  # ask the executor what became of it
                    else:
                        self._absorb_result(TurnResult(item.turn_id, item.session_id, TurnOutcome.FAILED, error=item.error))
        return moved

    def _absorb_event(self, event: TurnEvent) -> None:
        turn = self.store.turn(event.turn_id)
        if turn is None or turn["status"] in TERMINAL_TURN:
            return
        self.store.update_turn(event.turn_id, last_event_at=self.now())
        if event.type == EventType.STARTED and event.data.get("native_session_id"):
            self.store.update_assignment(turn["assignment_id"], native_session_id=event.data["native_session_id"])
        elif event.type == EventType.INBOX:
            try:
                entry = entry_from_wire(event.data)
            except (KeyError, ValueError, TypeError):
                return  # a malformed claim is no claim
            self._absorb_entry(turn, entry)
        elif event.type == EventType.RECALL_REQUEST:
            assignment = self.store.assignment(turn["assignment_id"])
            request_id = str(event.data.get("request_id"))
            text = self.recall(turn["ticket_id"], str(event.data.get("object_id")))
            payload = {"session_id": assignment.session_id if assignment else "", "request_id": request_id, "text": text}
            self.outbox.enqueue("answer_recall", f"recall:{event.turn_id}:{request_id}", payload, turn["ticket_id"])

    def _absorb_entry(self, turn: dict, entry: InboxEntry) -> None:
        """A worker's tool call. Stored once by (turn, seq); replays and retries are no-ops.
        Replies and subscriptions act at once. A report acts when its turn ends."""
        ticket_id = turn["ticket_id"]
        if not self.store.add_entry(turn["id"], entry.seq, ticket_id, entry.kind, to_wire(entry), self.now()):
            return
        ticket = self.store.ticket(ticket_id)
        assignment = self.store.assignment(turn["assignment_id"])
        if ticket is None or assignment is None or ticket.stopped or assignment.role != Role.BUILDER:
            return
        if entry.kind == EntryKind.REPLY:
            self._apply(tickets.worker_reply(ticket, entry.purpose, entry.text, self.policy))
        elif entry.kind == EntryKind.FOLLOW and entry.thread.startswith(ticket.repo + "#"):
            self.store.subscribe(ticket_id, entry.thread, SubMode.CONTEXT)
        elif entry.kind == EntryKind.UNRELATED:
            if dict(self.store.subs_of_ticket(ticket_id)).get(entry.thread) == SubMode.CONTEXT:
                self.store.unsubscribe(ticket_id, entry.thread)
        elif entry.kind == EntryKind.HANDBACK:
            self._handback(ticket, entry)

    def _handback(self, ticket: Ticket, entry: InboxEntry) -> None:
        """§5: only a person's message this Ticket actually received can go back to the Router."""
        trigger = self.store.trigger(entry.trigger)
        if trigger is None or trigger.kind not in PERSON_KINDS or not self.store.delivered_ref(ticket.id, trigger.id):
            return
        routing = self.store.routing(trigger.id)
        if routing is None or routing["status"] != "delivered":
            return  # the Ticket's own request can't go back, and nothing goes back twice at once
        self.store.repend_trigger(trigger.id, hint="handback")

    def _absorb_result(self, result: TurnResult) -> bool:
        turn = self.store.turn(result.turn_id)
        if turn is None or turn["status"] in TERMINAL_TURN:
            return False
        for entry in result.entries:
            self._absorb_entry(turn, entry)
        now = self.now()
        self.store.update_turn(
            result.turn_id,
            status="done",
            outcome=result.outcome,
            tokens=result.tokens,
            wall_seconds=result.wall_seconds,
            error=result.error[:2000],
            ended_at=now,
        )
        assignment = self.store.assignment(turn["assignment_id"])
        ticket = self.store.ticket(turn["ticket_id"])
        assert assignment is not None and ticket is not None
        self._gate_next.pop(ticket.id, None)
        if result.native_session_id:
            self.store.update_assignment(assignment.id, native_session_id=result.native_session_id)
        if result.outcome == TurnOutcome.ACCOUNT_LIMIT and self.accounts:
            self.accounts.mark_limited(turn["request"]["account"])
        report = self._turn_report(result.turn_id)
        self._apply(
            tickets.turn_ended(ticket, assignment.role, assignment.id, assignment.worktree.pinned_sha, result, report, now),
            turn_id=result.turn_id,
        )
        self._heard_meanwhile(ticket.id, turn["started_at"])
        self.outbox.enqueue("forget_turn", f"forget:{result.turn_id}", {"turn_id": result.turn_id}, ticket.id)
        return True

    def _heard_meanwhile(self, ticket_id: str, since: float) -> None:
        """The owner wrote while the turn ran, and the turn ended asking them something. Their words are queued and they think they have spoken,
        so waiting for him would wait forever: they go to the builder now. Found on the first real issue."""
        t = self.store.ticket(ticket_id)
        builder = self.store.live_builder(ticket_id)
        if t is None or builder is None or not t.is_open or t.phase != Phase.WAITING or t.waiting_reason != WaitReason.QUESTION:
            return
        for row in self.store.pending_inputs(builder.id):
            # Only what arrived during this turn: a failed turn's own inputs are queued again, and they've been heard.
            trigger = self.store.trigger(row["ref"]) if row["ref"] and row["wake"] and row["created_at"] >= since else None
            if trigger is not None and trigger.kind in PERSON_KINDS:
                self._apply(tickets.heard_meanwhile(t))
                return

    def _turn_report(self, turn_id: str) -> Report | None:
        chosen: Report | None = None
        for row in self.store.entries(turn_id=turn_id):
            if row["kind"] != EntryKind.REPORT or not row["body"].get("report"):
                continue
            try:
                chosen_now = report_from_wire(row["body"]["report"])
            except (KeyError, ValueError, TypeError):
                continue
            if chosen_now.status != "progress":
                chosen = chosen_now
        return chosen

    # --- restart ---------------------------------------------------------------------

    def recover(self) -> int:
        """§8. After any restart: for each turn we think is running, ask the executor. Alive means
        attach. Finished means take the result. Otherwise it was cut off, which means a recovery
        note. If the executor can't answer yet, try again next tick."""
        if self.executor is None:
            self._needs_recovery = False
            return 0
        turns = self.store.turns(statuses=("running",))
        try:
            alive = {r.turn_id for r in self.executor.running()}
            moved = 0
            for turn in turns:
                if self.runner.is_live(turn["id"]):
                    continue
                moved += 1
                if turn["id"] in alive:
                    self.runner.launch(self.executor, self._stored_request(turn))
                    continue
                result = self.executor.collect(turn["id"])
                if result is None:
                    assignment = self.store.assignment(turn["assignment_id"])
                    session = assignment.session_id if assignment and assignment.session_id else ""
                    result = TurnResult(turn["id"], session, TurnOutcome.INTERRUPTED, error="the executor has no record of this turn")
                with self.store.tx():
                    self._absorb_result(result)
        except ExecutorError:
            return 0  # still flagged
        self._needs_recovery = False
        return moved

    def _stored_request(self, turn: dict) -> TurnRequest:
        request = turn_request_from_wire(turn["request"])
        assignment = self.store.assignment(turn["assignment_id"])
        session = assignment.session_id if assignment and assignment.session_id else request.session_id
        return replace(request, session_id=session)

    # --- gates ----------------------------------------------------------------------

    def advance_gates(self) -> int:
        """One fresh read per Ticket with a PR, outside any transaction, then one transition.
        Skipped while its builder is at work: its turn's end decides what comes next."""
        if self.gates is None:
            return 0
        moved, policy, now = 0, self.policy, self.now()
        for ticket in self.store.tickets(status="open"):
            if ticket.quarantined or ticket.held or (ticket.pr is None and ticket.pr_claim is None):
                continue  # a hold pauses outside writes too, like updating the branch
            if self._builder_busy(ticket) or now < self._gate_next.get(ticket.id, 0.0):
                continue
            self._gate_next[ticket.id] = now + self.gate_read_seconds
            try:
                facts = read_facts(self.gates, ticket, policy)
                # §8: an outage's casualty isn't the builder's failure. Rerun it once, tell nobody.
                if self._rerun_outage_jobs(ticket, facts):
                    moved += 1
                    continue
            except Exception as exc:
                log.warning("gates on %s: %s", ticket.id, exc)
                continue  # the forge is down: decide nothing, read again next tick
            if facts is None:
                continue
            with self.store.tx():
                tr = tickets.gate_step(ticket, facts, policy, now)
                if tr.ticket != ticket or tr.effects or tr.inputs or tr.subscribe or tr.triggers or tr.start_reviewer_sha:
                    self._apply(tr)
                    moved += 1
        return moved

    def _rerun_outage_jobs(self, t: Ticket, facts: Any) -> bool:
        """True if a job on this head was killed by the last reboot and has just been queued for
        its one rerun. The Outbox key is the job id, so a second failure is an ordinary red."""
        killed = getattr(self.gates, "killed_jobs", None)
        if killed is None or self.boot_time is None or self.now() - self.boot_time > BOOT_RERUN_WINDOW_SECONDS:
            return False
        if facts is None or facts.pr is None or facts.ci is None or str(facts.ci.state) == "green":
            return False
        fresh = False
        for run_id, job_id in killed(t.repo, facts.pr.head_sha, self.boot_time):
            payload = {"repo": t.repo, "run_id": run_id, "job_id": job_id}
            with self.store.tx():
                fresh = self.outbox.enqueue("ci_rerun", f"ci_rerun:{t.repo}:{job_id}", payload, t.id) or fresh
        return fresh

    # --- cleanup ----------------------------------------------------------------------

    def clean_up(self) -> int:
        """Disk doesn't grow forever. Retired sessions go at once. A Ticket that ended keeps its
        worktree for `retain_ended_seconds`, so a later comment resumes it in place (§5.8); after
        that the pushed branch is what survives, and a resume starts from it."""
        if self.retain_ended_seconds is None or self.now() < self._cleanup_next:
            return 0
        now, moved = self.now(), 0
        self._cleanup_next = now + CLEANUP_EVERY_SECONDS
        with self.store.tx():
            for a in self.store.retired_sessions():
                # A replaced builder's worktree is its successor's; a reviewer's is its own.
                moved += self._remove_session(a, worktree=a.role == Role.REVIEWER)
            for a in self.store.ended_builders(now - self.retain_ended_seconds):
                moved += self._remove_session(a, worktree=True)
        return moved

    def _remove_session(self, a: Assignment, worktree: bool) -> int:
        payload = {"session_id": a.session_id, "worktree": worktree, "role": str(a.role)}
        return int(self.outbox.enqueue("remove_session", f"remove_session:{a.id}", payload, a.ticket_id))

    def _builder_busy(self, t: Ticket) -> bool:
        builder = self.store.live_builder(t.id)
        if builder is None:
            return False
        active = self.store.conn.execute(
            "SELECT 1 FROM turns WHERE assignment_id = ? AND status IN ('launching', 'running') LIMIT 1", (builder.id,)
        ).fetchone()
        if active:
            return True
        if t.phase == Phase.WAITING:
            return False
        return any(r["wake"] for r in self.store.pending_inputs(builder.id))

    # --- recall (§10) -----------------------------------------------------------------

    def recall(self, ticket_id: str, object_id: str) -> str:
        """Read a Trigger, Thread, Ticket or Report, within what this Ticket may see. What wasn't
        admitted stays unreadable; the answer is fenced, with its neighbours' ids."""
        missing = f"Nothing readable with id {object_id}."
        ticket = self.store.ticket(ticket_id)
        if ticket is None:
            return missing
        subs = dict(self.store.subs_of_ticket(ticket_id))
        if object_id == ticket_id:
            turns = [t["id"] for t in self.store.turns(ticket_id=ticket_id)]
            threads = list(subs)
            summary = f"status={ticket.status} phase={ticket.phase} pr={ticket.pr} head={ticket.head_sha}"
            return f"Ticket {ticket_id}: {summary}\nthreads: {threads}\nturns: {turns}"
        if object_id in subs:
            rows = self.store.conn.execute(
                "SELECT t.id FROM triggers t JOIN trigger_routing r ON r.trigger_id = t.id "
                "WHERE t.thread = ? AND r.status != 'not_admitted' ORDER BY t.seq",
                (object_id,),
            ).fetchall()
            return f"Thread {object_id}: triggers {[r['id'] for r in rows]}"
        trigger = self.store.trigger(object_id)
        if trigger is not None:
            routing = self.store.routing(object_id)
            visible = trigger.thread in subs or self.store.delivered_ref(ticket_id, object_id)
            if not visible or routing is None or routing["status"] == "not_admitted":
                return missing
            return cards.fence(
                _recall_text(trigger), author=_recall_author(trigger), kind=str(trigger.kind), ref=trigger.id,
                neighbours=(ticket_id, trigger.thread),
            )
        for row in self.store.entries(ticket_id=ticket_id):
            if f"{row['turn_id']}:{row['seq']}" == object_id:
                return cards.fence(json.dumps(row["body"], sort_keys=True), author="worker", kind=row["kind"], ref=object_id, neighbours=(ticket_id, row["turn_id"]))
        return missing

    # --- the command spool -------------------------------------------------------------

    def drain_spool(self) -> int:
        """Commands from the CLI while we hold the writer lock. Each file is applied once, then
        deleted; if we crash in between, its id makes the replay a no-op."""
        if not self.spool_dir or not os.path.isdir(self.spool_dir):
            return 0
        moved = 0
        for name in sorted(os.listdir(self.spool_dir)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.spool_dir, name)
            try:
                with open(path, encoding="utf-8") as f:
                    apply_command(self, json.load(f))
            except (ValueError, KeyError, TypeError):  # includes PolicyError
                os.rename(path, path + ".rejected")  # one bad file mustn't stop the factory
            else:
                os.unlink(path)
            moved += 1
        return moved


def apply_command(engine: Engine, command: dict) -> None:
    """One operator command: from the CLI directly, or from the spool."""
    kind, actor = command["command"], command["actor"]
    if kind == "policy":
        check_removal(engine.store, command)
        engine.set_policy(policy_from_command(engine.policy, command), actor)
    else:
        engine.command(kind, command["ticket"], actor)


def check_removal(store, command: dict) -> None:
    """A repository leaves the policy only with no open Ticket on it: its gates and turns read the repo's policy."""
    name = command.get("remove_repo")
    if not name:
        return
    busy = [t.id for t in store.tickets("open") if t.repo == name]
    if busy:
        raise PolicyError(f"{name} has open tickets ({', '.join(busy)}); stop or finish them first")


def policy_from_command(policy: Policy, command: dict) -> Policy:
    """The new policy a command asks for, validated. Raises PolicyError."""
    if command.get("add_repo"):
        r = command["add_repo"]
        policy = policy.with_repository(RepoPolicy(
            name=str(r["name"]),
            base=str(r.get("base") or "main"),
            required_checks=tuple(str(c) for c in r.get("required_checks") or ("CI / checks (pull_request)",)),
            ci_runner=str(r.get("ci_runner") or ""),
            carry_review_across_main=bool(r.get("carry_review_across_main", False)),
            data_rules=str(r.get("data_rules") or "").strip(),
        ))
    if command.get("remove_repo"):
        policy = policy.without_repository(str(command["remove_repo"]))
    if command.get("repo") and "data_rules" in command:
        policy = policy.with_repo_data_rules(command["repo"], str(command["data_rules"]))
    if command.get("rule"):
        policy = policy.with_rule(command["rule"], RuleMode(command["mode"]))
    if command.get("source"):
        policy = policy.with_allowed(command["source"], tuple(command["people"]))
    if command.get("repo") and "carry_review" in command:
        policy = policy.with_carry_review(command["repo"], bool(command["carry_review"]))
    if "slots" in command:
        policy = policy.with_slots(int(command["slots"]), str(command.get("tight_account") or ""), float(command.get("tight_pct", 70)), int(command.get("tight_slots", 2)))
    if command.get("repo") and "ci_runners" in command:
        policy = policy.with_ci_runners(command["repo"], tuple(command["ci_runners"]))
    policy.validate()
    return policy


def spool_command(spool_dir: str, command: dict) -> str:
    """Atomically drop a command for the running writer."""
    os.makedirs(spool_dir, mode=0o770, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=spool_dir, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(command, f)
    path = os.path.join(spool_dir, f"{time.time():.6f}-{uuid.uuid4().hex}.json")
    os.rename(tmp, path)
    return path

