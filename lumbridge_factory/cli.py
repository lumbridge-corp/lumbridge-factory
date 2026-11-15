"""`lumbridge-factory status|tickets|show|stop|hold|retry|policy|demo`.

Reads open the store read-only. Writes obey the one-writer rule: if nobody holds the writer lock
(a scratch store) the CLI is the writer and applies the command itself; if factoryd holds it, the
command goes through a spool directory that factoryd drains on its next tick.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from typing import Any

from . import snapshot
from .core.policy import PolicyError, default_policy
from .engine import Engine, apply_command, check_removal, policy_from_command, spool_command
from .store import Store, StoreError, WriterBusy
from .names import NEEDS_KEY

DEFAULT_DB = "factory.sqlite3"


def _spool_dir(db: str) -> str:
    return db + ".commands"


def _ensure_store(db: str, instance: str) -> None:
    """A missing store is created with the default policy, the owner only. factoryd installs a real
    instance's policy; this is for scratch use."""
    if os.path.exists(db):
        return
    store = Store.writer(db)
    try:
        Engine(store).bootstrap(default_policy(instance))
    finally:
        store.close()


def _reader(db: str) -> Store:
    return Store.reader(db)


def _write(db: str, command: dict[str, Any]) -> str:
    command = {**command, "actor": command.get("actor") or f"cli:{getpass.getuser()}"}
    if command["command"] == "policy":  # refuse here, not later and silently in factoryd
        store = _reader(db)
        try:
            check_removal(store, command)
            policy_from_command(store.policy(), command)
        finally:
            store.close()
    try:
        store = Store.writer(db)
    except WriterBusy:
        spool_command(_spool_dir(db), command)
        return "queued for factoryd"
    try:
        apply_command(Engine(store), command)
    finally:
        store.close()
    return "done"


def _print_json(value: Any) -> None:
    json.dump(value, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _ticket_line(v: dict[str, Any]) -> str:
    flags = "".join(f" [{f}]" for f in ("held", "quarantined") if v[f])
    state = v["end_reason"] if v["status"] == "ended" else v["phase"]
    pr = f"PR #{v['pr']}" if v["pr"] else "no PR"
    return f"{v['id']}  {state:<9} {v['repo']}  {pr:<9} {v['title'][:50]}{flags}"


def cmd_status(args: argparse.Namespace) -> int:
    _ensure_store(args.db, args.instance)
    store = _reader(args.db)
    try:
        snap = snapshot.build(store, time.time())
    finally:
        store.close()
    if args.json:
        _print_json(snap)
        return 0
    cap, effects = snap["capacity"], snap["effects"]
    print(f"{snap['instance']}: {len(snap['tickets'])} tickets, policy v{snap['policy_version']}, schema v{snap['schema_version']}")
    print(f"slots: {cap['used']}/{cap['slots']} in use, reserved {cap['used_reserved']}/{cap['reserved_slots']}")
    print(f"outbox: {effects.get('pending', 0)} pending, {effects.get('failing', 0)} failing, {effects.get('uncertain', 0)} uncertain")
    by_id = {v["id"]: v for v in snap["tickets"]}
    if snap[NEEDS_KEY]:
        print("\nNeeds you:")
        for id in snap[NEEDS_KEY]:
            v = by_id[id]
            print(f"  {_ticket_line(v)}")
            if v[NEEDS_KEY] == "ready_to_merge":
                print("    ready to merge")
            elif v["question"]:
                print(f"    {v['question']}")
    if snap["working"]:
        print("\nWorking:")
        for id in snap["working"]:
            print(f"  {_ticket_line(by_id[id])}")
    if not snap[NEEDS_KEY] and not snap["working"]:
        print("\nNothing needs you.")
    if snap["feed"]:
        print("\nFeed:")
        for item in snap["feed"]:
            print(f"  {item['thread']}: {item['text']}")
    return 0


def cmd_tickets(args: argparse.Namespace) -> int:
    _ensure_store(args.db, args.instance)
    store = _reader(args.db)
    try:
        snap = snapshot.build(store, time.time())
    finally:
        store.close()
    views = snap["tickets"] if args.all else [v for v in snap["tickets"] if v["status"] == "open"]
    if args.json:
        _print_json(views)
    elif not views:
        print("No tickets." if args.all else "No open tickets. --all shows ended ones.")
    else:
        for view in views:
            print(_ticket_line(view))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = _reader(args.db)
    try:
        t = store.ticket(args.ticket)
        if t is None:
            print(f"No ticket {args.ticket}.", file=sys.stderr)
            return 1
        mandate = store.mandate(t.mandate_id)
        view = {
            **snapshot.ticket_view(store, t),
            "mandate": {
                "id": mandate.id,
                "strength": str(mandate.strength),
                "requested_by": mandate.requested_by,
                "rules": list(mandate.rules),
                "policy_version": mandate.policy_version,
            }
            if mandate
            else None,
            "subscriptions": {thread: str(mode) for thread, mode in store.subs_of_ticket(t.id)},
            "assignments": [
                {"id": a.id, "role": str(a.role), "live": a.live, "account": a.account, "model": a.model, "session": a.session_id, "native_session": a.native_session_id}
                for a in store.assignments(t.id)
            ],
            "turns": [
                {"id": turn["id"], "status": turn["status"], "outcome": turn["outcome"], "tokens": turn["tokens"], "slot": turn["slot"], "error": turn["error"]}
                for turn in store.turns(ticket_id=t.id)
            ],
            "reports": [
                row["body"]["report"]
                for row in store.entries(ticket_id=t.id)
                if row["kind"] == "report" and row["body"].get("report")
            ],
        }
    finally:
        store.close()
    if args.json:
        _print_json(view)
        return 0
    print(_ticket_line(view))
    print(f"thread: {view['thread']}   branch: {view['branch']}")
    if view["question"]:
        print(f"question: {view['question']}")
    if view["mandate"]:
        m = view["mandate"]
        print(f"mandate: {m['strength']}, asked by {m['requested_by']}, rules {','.join(m['rules'])}, policy v{m['policy_version']}")
    print(f"budget: {view['tokens']}/{view['budget_tokens']} tokens, {view['wall_seconds']}/{view['budget_wall_seconds']} s; review rounds {view['review_rounds']}")
    print("subscriptions:")
    for thread, mode in view["subscriptions"].items():
        print(f"  {thread} ({mode})")
    print("turns:")
    for turn in view["turns"]:
        print(f"  {turn['id']}  {turn['status']}  {turn['outcome'] or '-'}  {turn['tokens']} tokens")
    return 0


def _ticket_command(name: str):
    def run(args: argparse.Namespace) -> int:
        store = _reader(args.db)
        try:
            known = store.ticket(args.ticket) is not None
        finally:
            store.close()
        if not known:
            print(f"No ticket {args.ticket}.", file=sys.stderr)
            return 1
        kind = "release" if name == "hold" and args.release else name
        print(f"{kind} {args.ticket}: {_write(args.db, {'command': kind, 'ticket': args.ticket})}")
        return 0

    return run


def cmd_policy(args: argparse.Namespace) -> int:
    _ensure_store(args.db, args.instance)
    if args.action == "set-mode":
        print(f"policy: {_write(args.db, {'command': 'policy', 'rule': args.rule, 'mode': args.mode})}")
        return 0
    if args.action == "allow":
        print(f"policy: {_write(args.db, {'command': 'policy', 'source': args.source, 'people': args.people})}")
        return 0
    if args.action == "slots":
        command = {"command": "policy", "slots": args.slots, "tight_account": args.tight_account or "", "tight_pct": args.tight_pct, "tight_slots": args.tight_slots}
        print(f"policy: {_write(args.db, command)}")
        return 0
    if args.action == "ci-runners":
        print(f"policy: {_write(args.db, {'command': 'policy', 'repo': args.repo, 'ci_runners': args.names})}")
        return 0
    if args.action == "add-repo":
        repo = {"name": args.repo, "base": args.base, "required_checks": args.check or ["CI / checks (pull_request)"],
                "ci_runner": ",".join(args.runner), "carry_review_across_main": args.carry_review, "data_rules": args.data_rules}
        print(f"policy: {_write(args.db, {'command': 'policy', 'add_repo': repo})}")
        return 0
    if args.action == "remove-repo":
        print(f"policy: {_write(args.db, {'command': 'policy', 'remove_repo': args.repo})}")
        return 0
    if args.action == "data-rules":
        print(f"policy: {_write(args.db, {'command': 'policy', 'repo': args.repo, 'data_rules': args.text})}")
        return 0
    if args.action == "carry-review":
        print(f"policy: {_write(args.db, {'command': 'policy', 'repo': args.repo, 'carry_review': args.state == 'on'})}")
        return 0
    store = _reader(args.db)
    try:
        policy = store.policy()
        history = store.policy_history()
        observed = {
            rule: {
                "recorded": len(store.evals(rule)),
                "would_accept": sum(1 for e in store.evals(rule) if e["would_accept"]),
            }
            for rule in ("main-red", "triage")
        }
    finally:
        store.close()
    assert policy is not None
    if args.json:
        _print_json({"policy": policy.to_dict(), "history": history, "observed": observed})
        return 0
    print(f"{policy.instance}, policy v{policy.version}")
    print("may ask for work:")
    for source, people in policy.allowed.items():
        print(f"  {source}: {', '.join(people) or '-'}")
    print("repositories:")
    for repo in policy.repositories:
        carry = "; a review carries across a clean main merge" if repo.carry_review_across_main else ""
        runners = f"; from runner(s) {repo.ci_runner}" if repo.ci_runner else ""
        print(f"  {repo.name} ({repo.base}), checks: {', '.join(repo.required_checks)}{runners}{carry}")
        if repo.data_rules:
            print(f"    data: {repo.data_rules}")
    print("rules:")
    for rule, mode in policy.rules.items():
        extra = f"  ({observed[rule]['recorded']} recorded, {observed[rule]['would_accept']} would accept)" if rule in observed else ""
        print(f"  {rule}: {mode}{extra}")
    tight = f" ({policy.tight_slots} once {policy.tight_account} passes {policy.tight_pct:g} % of its allowance)" if policy.tight_account else ""
    print(f"slots: {policy.slots}{tight} + {policy.reserved_slots} reserved; {policy.max_review_rounds} review rounds")
    print(f"merge: {policy.merge_policy}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Fill a scratch store with one fake Ticket per state, to see the CLI and the snapshot."""
    from .demo import run_demo

    if os.path.exists(args.db):
        print(f"{args.db} exists; demo wants a new scratch path.", file=sys.stderr)
        return 1
    run_demo(args.db)
    print(f"Wrote {args.db}. Try: lumbridge-factory --db {args.db} status")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lumbridge-factory", description="Lumbridge Factory: issues in, reviewed pull requests out")
    # FACTORY2_DB is the variable's name before 0.1.0, still read when the new one isn't set.
    db = os.environ.get("LUMBRIDGE_FACTORY_DB") or os.environ.get("FACTORY2_DB") or DEFAULT_DB
    parser.add_argument("--db", default=db, help="store path (env LUMBRIDGE_FACTORY_DB)")
    parser.add_argument("--instance", default="scratch", help="instance name for a new scratch store")
    parser.add_argument("--json", action="store_true", help="print JSON")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="what needs you, what's working").set_defaults(run=cmd_status)
    p = sub.add_parser("tickets", help="list tickets")
    p.add_argument("--all", action="store_true", help="include ended tickets")
    p.set_defaults(run=cmd_tickets)
    p = sub.add_parser("show", help="one ticket in full")
    p.add_argument("ticket")
    p.set_defaults(run=cmd_show)
    p = sub.add_parser("stop", help="stop a ticket; withdraws its Mandate for good")
    p.add_argument("ticket")
    p.set_defaults(run=_ticket_command("stop"))
    p = sub.add_parser("hold", help="start no new turn on a ticket; --release undoes it")
    p.add_argument("ticket")
    p.add_argument("--release", action="store_true")
    p.set_defaults(run=_ticket_command("hold"))
    p = sub.add_parser("retry", help="clear a question or quarantine and try again")
    p.add_argument("ticket")
    p.set_defaults(run=_ticket_command("retry"))

    p = sub.add_parser("policy", help="show or change the policy")
    actions = p.add_subparsers(dest="action")
    q = actions.add_parser("set-mode", help="set a rule's mode")
    q.add_argument("rule")
    q.add_argument("mode", choices=["off", "observe", "auto"])
    q = actions.add_parser("allow", help="replace who may ask for work on a source")
    q.add_argument("source")
    q.add_argument("people", nargs="*")
    q = actions.add_parser("slots", help="how many turns at once, and a lower number once one account is running low")
    q.add_argument("slots", type=int)
    q.add_argument("--tight-account", default="", help="the account whose reported use lowers it")
    q.add_argument("--tight-pct", type=float, default=70.0)
    q.add_argument("--tight-slots", type=int, default=2)
    q = actions.add_parser("ci-runners", help="the CI runners whose green counts for one repo")
    q.add_argument("repo")
    q.add_argument("names", nargs="+")
    q = actions.add_parser("add-repo", help="take Tickets on one more repository (its runner, protection, bots and agentd entry come first)")
    q.add_argument("repo", help="owner/name")
    q.add_argument("--base", default="main")
    q.add_argument("--check", action="append", help="a required check; default CI / checks (pull_request)")
    q.add_argument("--runner", action="append", required=True, help="a CI runner whose green counts; repeat for several")
    q.add_argument("--carry-review", action="store_true", help="carry a passing review across a clean base merge")
    q.add_argument("--data-rules", default="", help="what this repo adds to the instance's data rules")
    q = actions.add_parser("remove-repo", help="take no more Tickets on a repository; refused while one is open there")
    q.add_argument("repo")
    q = actions.add_parser("data-rules", help="replace one repository's own data rules")
    q.add_argument("repo")
    q.add_argument("text")
    q = actions.add_parser("carry-review", help="carry a passing review across a clean main merge, for one repo (docs/ARCHITECTURE.md, Gate 3)")
    q.add_argument("repo")
    q.add_argument("state", choices=["on", "off"])
    p.set_defaults(run=cmd_policy)

    sub.add_parser("demo", help="fill a new scratch store with fake tickets").set_defaults(run=cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.run(args)
    except (StoreError, PolicyError) as exc:
        print(f"lumbridge-factory: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
