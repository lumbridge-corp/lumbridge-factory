# factoryd ↔ agentd protocol (version 1)

The contract for ARCHITECTURE §3 and §6. The Python shapes are in `lumbridge_factory/core/executor.py`, which both sides import;
use its `*_from_wire` functions and `to_wire`. This file adds the socket and the behaviour the types can't state.

factoryd holds all domain state. agentd holds worktrees, agent session stores, and a small operational journal of turns.

## Transport

- Unix socket `/run/<name>-factory/agentd.sock` (owner `<name>-factory`, group `<name>-control`, mode 0660). factoryd
  always connects; agentd never calls factoryd.
- Newline-delimited JSON, UTF-8. One request per connection.
- Request: `{"v": 1, "op": "<op>", "args": {...}}`
- Reply: `{"ok": true, "result": ...}` or `{"ok": false, "error": {"code": "...", "message": "...", "retryable": bool}}`
- A wrong `v` gets `{"ok": false, "error": {"code": "protocol_mismatch", "retryable": false}}`.
- `turn` is the only streaming op. Before its reply it sends zero or more `{"event": <TurnEvent>}` lines, then
  `{"ok": true, "result": <TurnResult>}`.
- Error codes: `bad_request`, `protocol_mismatch`, `unknown_session`, `unavailable`, `internal`. On the factoryd side they
  surface as `ExecutorError(code, message, retryable)`.

## Ops

| op | args | result | Rule |
|---|---|---|---|
| `hello` | | `{"protocol": 1, "tools": ["claude", "codex"]}` | Health check |
| `start` | `StartRequest` | `{"session_id": str}` | Idempotent on `assignment`. Prepares the worktree, pre-trusts it, writes the ticket's MCP and settings files. **Runs no model** |
| `turn` | `TurnRequest` | stream, then `TurnResult` | Idempotent on `turn_id`, see below |
| `stop` | `{"session_id": str}` | `{}` | Kills a running turn; that turn ends `stopped`. Idempotent; fine if idle |
| `running` | | `[RunningTurn]` | Every turn alive right now |
| `collect` | `{"turn_id": str}` | `TurnResult` or `null` | `null` = unfinished or never seen |
| `forget` | `{"turn_id": str}` | `{}` | factoryd has stored the result; agentd may delete it |
| `answer_recall` | `{"session_id", "request_id", "text"}` | `{}` | Hands a `recall` answer to the waiting MCP tool |
| `remove_session` | `{"session_id": str, "worktree": bool}` | `{}` | Deletes the session's files and, if `worktree`, its worktree (read-only ones too), local branch, the CLI's session store and pre-trust entry. Idempotent; unknown is fine. Retryable `unavailable` while a turn runs there. Added without a `v` change |

## Turn idempotency

Before spawning anything, agentd durably writes the `turn_id` and request to its journal, and writes the `TurnResult` when
the turn ends. `turn` with a known `turn_id` never spawns a second process:

| The turn is | `turn` does |
|---|---|
| never seen | Journals it and starts it |
| running | Attaches: replays events so far, then follows live |
| finished | Replays events if kept (it may send none), then the result |
| cut off by an agentd restart (in the journal, no result) | Writes and replays a result with outcome `interrupted` |

So at startup agentd turns every journal entry without a result into `interrupted`. factoryd then gives that Ticket a new
turn with a new `turn_id` and a recovery note. agentd never retries or resumes a turn on its own.

A dropped client connection doesn't stop a turn. Only `stop`, the wall-clock limit or process death does.

## Session ids

`start` returns `session_id`, agentd's handle for the Assignment. The agent CLI's own id (from claude's `init`/`result`
event, codex's `--json` events) is the `native_session_id`. Return it in a `started` event as soon as it is known and in
`TurnResult`. factoryd stores it and sends it back on the next `TurnRequest`, where `null` means first turn and a value
means `--resume`. If resuming fails, return outcome `resume_failed`; factoryd makes a new Assignment in the same worktree.
Don't fall back to a fresh session silently.

## Delivering inputs

`TurnRequest.inputs` are rendered by factoryd and already fenced. Join their `text` in order, separated by a blank line, and
pass the result as the turn's user message, verbatim. Never put them in the system prompt. `PromptParts` is the only trusted
text: `system` then `rule_instructions` go into `--append-system-prompt-file`. The session's first turn carries the
assignment as its first input.

`account` and `model` are per turn, because the account router can switch between turns. Render that account's token into the
child's environment only (§12).

## Outcomes

| Outcome | Use when | factoryd then |
|---|---|---|
| `completed` | The model ended its turn (exit 0, final result event) | Reads the inbox |
| `interrupted` | Killed by a restart or signal not from `stop` | New turn with a recovery note |
| `transient` | HTTP 408/429/5xx, connection reset, overload | Same inputs, backoff; 3 retries, then a question to the owner |
| `account_limit` | The account's limit | Same inputs on another account; waits if none |
| `resume_failed` | The native session can't be resumed | New Assignment, same worktree |
| `timeout` | `limit_seconds` passed; agentd killed it | New turn with a recovery note |
| `failed` | Anything else; put a short tail in `error` | One question to the owner |
| `stopped` | `stop` was called | Nothing more |

Classify with evidence from the CLI's output. `error` is for a person to read and is never parsed. If unsure, use `failed`.

## The worker's tools and the inbox

The `factory-tools` MCP server (§6) appends each tool call to the Assignment's inbox. agentd assigns `seq` from 1 per turn,
emits each as `{"type": "inbox", "data": <InboxEntry>}` as it happens, and puts the complete list in `TurnResult.entries`. A
report that doesn't parse is dropped by the tool with an error back to the model, so it can correct itself.

| Tool | Entry |
|---|---|
| `report(status, summary, pr?, question?, verdict?, findings?)` | `kind: report`, `report: {...}`. `verdict` and `findings` are for reviewers |
| `reply(purpose, text)` | `kind: reply` |
| `follow(thread)` / `unrelated(thread)` | `kind: follow` / `unrelated`, `thread` |
| `handback(trigger, reason)` | `kind: handback`, `trigger`, `text` = reason |
| `recall(id)` | Not an entry. Emit `{"type": "recall_request", "data": {"request_id", "object_id"}}`; block the tool until `answer_recall` arrives, time out after 60 s |

Everything in the inbox is a claim. factoryd re-checks it; agentd doesn't need to.

Only a builder's `reply`, `follow`, `unrelated` and `handback` take effect; a reviewer's only output is its `report` with `verdict` and
`findings`. Offer reviewers just `report` and `recall`. A running turn's `reply` is posted as soon as its event arrives, and a `report` acts
when the turn ends.

Each turn should end with a `report`. If it doesn't, factoryd sends one reminder turn. That is factoryd's job, not agentd's.

## Client errors

The factoryd-side client must raise `ExecutorError` for every failure, including a refused or dropped socket (`unavailable`, retryable). A stream
that ends without a `TurnResult` makes factoryd call `running` and `collect`, then reattach or recover.

## What agentd must not do

Parse free text for state, decide a Ticket's fate, retry a turn, talk to Gitea except through git in a worktree, or read
factoryd's store. Reviewer worktrees are pinned to `pinned_sha` and read-only.

## Compatibility

Unknown keys are ignored on both sides. New optional fields don't change `v`; a changed meaning does.
