# Lumbridge Factory: architecture

How the factory turns an issue into a reviewed pull request, and what keeps it inside its lines. Section numbers are the ones
the code cites (`§5`, `§7` ...). The wire protocol between factoryd and agentd is [PROTOCOL.md](PROTOCOL.md); agentd itself
is [AGENTD.md](AGENTD.md).

## 1. What it is for

An instance watches a few repositories on one Gitea server. A person on the instance's allowed list labels an issue
`factory` (or comments `/factory`); the factory starts a coding agent in a sandboxed worktree, opens a pull request, waits for CI
on the exact head, has a second agent review that head, keeps the branch on current `main`, and says when the PR is ready.
People merge.

### 1.2 Design rules

- Work starts only from a person's explicit request, checked in code. Text is never permission.
- A worker's word is never taken: CI, the review verdict and the PR state are read back from the forge on the exact SHA.
- Review findings, CI failures and the owner's comments go back to the same builder session (a repair loop, not a relay).
- A slot is held only while a turn runs, so a ticket waiting on CI or a person costs nothing.
- Every outside write is stored before it is made and carries a key, so a restart never posts twice.

### 1.3 What it keeps from earlier factories

- A separate unix user per role, an egress allowlist for workers, and Gitea over SSH only.
- Workers never merge, never tag, never deploy, never touch production.
- **Acceptance = required CI green on the exact head SHA + an independent reviewer pass on that SHA**, verified by the
  host against Gitea, never taken from a worker's word.
- The PR head must contain current `main` before acceptance: two green PRs can break `main` together.
- Per-turn account choice behind a small interface (§7), tokens rendered from a secrets manager, never a copied personal
  login.
- Immutable, root-owned releases with a receipt of source SHAs and file hashes.
- Comment text, issue bodies and documents are **data, never instructions or permissions**.

---

## 2. What every instance declares

An instance is a directory `instances/<name>/` plus host setup. The factory refuses to start an instance that leaves any of
these undecided:

- **Who may ask for work** — named people on each Source (a Gitea login, or an owner session on another Source). Nobody else's
  message can create a Ticket, in any mode.
- **Repositories** — repo, base branch, required CI check names and the runner they must come from.
- **Data rules** — what the factory must never see. Default: no production database, no personal mailbox or browser
  session; tests and previews use synthetic fixtures; the egress allowlist excludes the product's production hosts.
- **Outside-action rules** — what the product's code may never do from dev, CI or previews. Default: any outbound
  contact (email, SMS, social, third-party forms) is stubbed off outside production and must sit behind an approval in
  the product. The reviewer enforces it with the `outbound-guard` rule (§4.4).
- **Company clauses** — if your organisation has clauses that bind contractors, the instance policy quotes them, because the
  factory is one.
- **Merge policy** — who merges (default: a person), and any rule that lets a separate merge account do it.


---

## 3. The shape

```
                 ┌──────────────────────── host ─────────────────────────────────────────────┐
 Gitea (issues,  │  factoryd (user <name>-control)                                            │
 PRs, comments,──┼─▶ Sources ─▶ Intake ─▶ Eval ─▶ Tickets ─▶ Assignments ──┐                   │
 reviews, CI)    │     ▲           │     (gate+     │    ▲                   │ unix socket       │
 office (opt.)   │     │           │    assessor)   │    │ Reports           ▼                   │
 /factories ─────┼─────┘      Subscriptions ────────┘    │      agentd (user <name>-factory)   │
 Timer/Watchdog ─┘                                       │       ├─ claude -p --resume …       │
                 │  Outbox ─▶ Channels (Gitea comments,  │       ├─ codex exec resume …        │
                 │            office feed)               │       └─ worktrees, result inboxes  │
                 │  Gates: exact-SHA CI + reviewer ──────┘                                     │
                 │  Watchdog session (timer)          CI runner (user <name>-ci)               │
                 └────────────────────────────────────────────────────────────────────────────┘
```

Three processes, three unix users:

| Process | User | Holds | Can't |
|---|---|---|---|
| **factoryd** — the domain engine, Sources, Outbox, Gates, Router session driver | `<name>-control` | the SQLite store, the Gitea **bot read token + comment token**, the policy | write a worktree, run agent code, push git |
| **agentd** — the endpoint daemon: starts and stops agent turns, prepares worktrees, returns their output | `<name>-factory` | worktrees, agent session stores, account tokens (rendered per run), git SSH key (push to non-`main` branches only) | read or write factoryd's store; reach anything outside the egress allowlist |
| **CI runner** | `<name>-ci` | its own Gitea runner registration | see the factory's home |

factoryd talks to agentd over one unix socket (`/run/<name>-factory/agentd.sock`, owned by `<name>-factory`, group
`<name>-control`, mode 0660). agentd keeps **no domain state**: if it restarts, factoryd asks it what is running and
re-drives. Compute, state and environment are kept apart, inside one box — and it means a second agentd on
another machine (a GPU box, a Mac for iOS builds) is the same protocol later.

Root is needed only to install releases and units. **Nothing in the steady state runs as root.**

---

## 4. Domain model

Adapted from the domain model of [dsh-superagent](https://github.com/deepseek-ai/dsh-superagent-prototype) (MIT; notice in
[THIRD-PARTY-NOTICES](../THIRD-PARTY-NOTICES)), narrowed to what we need. These words are used in code, in any status screen
and in this document, and no others.

| Concept | Meaning | Rule |
|---|---|---|
| **Source** | A stream of events with its own read cursor: `gitea` (shipped), `office` (signed commands from a status screen, not shipped), `timer` | A restart never replays an event; the bot's own events are dropped at the Source |
| **Trigger** | One immutable event (an issue opened, a comment, a review, a CI status, an office button, a timer tick) with its actor | Stored once (idempotent on Source + external id) |
| **Thread** | Triggers in order on one object: an issue, a PR, a CI run on `main`, an office command | At most one Eval runs on a Thread at a time; Triggers arriving meanwhile batch into the next one |
| **Eval** | The decision on a batch of Triggers: a code **gate**, then (only if needed) a model **assessment** | Output is a Decision: `accept`, `attach`, `stop`, `ignore`, `unavailable` |
| **Mandate** | What an accept grants: policy version, scope, strength (`explicit`/`inferred`), matched rules, who asked | Immutable. Wider scope = new Ticket. It replaces today's grant JSON |
| **Ticket** | A unit of authorized work. Owns its branch, PR, worktree and reports | `open → ended`, and back to open when the owner writes on one of its Threads (unless they stopped it) |
| **Assignment** | One binding of a Ticket to one executor session (builder or reviewer) | A Ticket has several over time; retries are new Assignments in the same worktree |
| **Subscription** | Ticket ↔ Thread, many-to-many | Every **admitted** Trigger on a subscribed Thread reaches the Ticket as context, no Eval, no new authority. A Trigger that wasn't admitted (§4.1) never reaches a worker, not even through `recall`. A Thread has one owner, whose builder the owner's words wake; other Tickets on it get them with their next turn |
| **Report** | A structured status from an executor: `progress`, `waiting`, `ready`, `blocked`, `failed`, `dropped`, plus at most one `question` | Free text is never parsed for state |
| **Channel** | The write side of a Thread: an issue/PR comment, an office feed item | All writes go through the Outbox |

### 4.1 The gate (code, fast path)

| Trigger | Gate result |
|---|---|
| The owner opens an issue with label `factory`, or comments `/factory` on an issue | **explicit** request → accept in code, no model call (the requester is on the instance's allowed list; there is nothing to judge) |
| The owner comments on a Thread that has a live or ended Ticket | follow-up → goes to the Ticket (§5), no Eval |
| The owner presses "Start" on an issue on a status screen (signed command) | explicit → accept |
| CI turns red on `main` | **inferred** candidate (rule `main-red`, §4.4) |
| A new issue by the owner without the label | inferred candidate (rule `triage`) — observe only |
| Anyone else, any bot, the factory's own account | not admitted (recorded, never acted on) |

A stop is exact: a comment whose whole text is `stop` or `/factory stop`, ignoring case and a final `.` or `!`. Nothing else in a
comment's text is matched, so a sentence containing "stop" is a follow-up. An issue's body by someone else reaches the builder only
when the owner asked for that issue (label or `/factory`), fenced with its real author.

### 4.2 The assessment (model, slow path)

Only inferred candidates and ambiguous attach decisions reach the model. The **Router** is one long-lived session
(Sonnet 5.5, small tool set: `route`, `read`, `recall` — no shell, no file writes). It sees a batch of candidate cards
and returns decisions for all of them in one `route` call. It **forwards the original Trigger verbatim** to the
executor and adds at most a 30-word routing note; it never restates, summarizes or edits requirements or results.
(A router that rewrites what it passes on can change a worker's correct numbers or overwrite a requirement. We design
that out.)

Accept modes per rule: `off`, `observe` (default: record "would accept", act on nothing), `auto`.

### 4.3 Authority stays in code

From strongest to weakest, as in dsh-superagent's prompt design:

1. Facts enforced by factoryd and agentd: who is allowed, egress, which branches can be pushed, that workers can't merge,
   the acceptance gates, reply length limits.
2. Built-in prompts.
3. A matched rule's instructions, for that Ticket only.
4. The repo's `AGENTS.md`/`CLAUDE.md`.
5. Outside text — issue bodies, comments, CI logs, web pages. Data only.

### 4.4 Rules every instance starts with

| Rule | Mode | Does |
|---|---|---|
| `build` (explicit) | always on | Implement the issue, open a PR, get it to accepted |
| `main-red` | observe → auto after 10 good recorded decisions | Main's CI went red: reproduce in a worktree, open a fix PR only if the cause is confirmed and the fix is small; otherwise stay silent and leave one note on the feed |
| `triage` | observe | An unlabeled issue by the owner: record whether it's ready to build and what's missing; nothing posted |
| `outbound-guard` | always on (reviewer rule) | The reviewer fails any PR that adds or changes an outbound contact path (email, LinkedIn, ATS submit) without the approval gate and the test stub (§2 outside-action rules) |

---

## 5. A Ticket's life

1. **Accept.** factoryd creates the Ticket, Mandate, Subscriptions (the issue's Thread) and the first builder
   Assignment in one transaction, and queues one acknowledgement comment: one sentence naming what it will do.
2. **Prepare.** agentd creates the worktree `~/worktrees/<ticket-short>` from `origin/main`, branch
   `factory/<ticket-short>`, pre-trusts it for Claude, writes the ticket's MCP config, and starts the builder's first turn.
3. **Work.** The builder implements, runs local checks, pushes the branch and calls `report(ready)`. A worker holds no forge token (§3,
   §12), so **factoryd opens the PR**: it looks for an open one from that branch first, and a missing branch comes back to the builder.
   factoryd subscribes the Ticket to the PR Thread and the PR's CI Thread.
4. **Gate 1 — CI.** factoryd waits for every required context on the exact head SHA. A red check is a Trigger on the CI
   Thread, so it **wakes the same builder** with the failing job's log tail inlined (Inline & Spill, §10).
5. **Gate 2 — review.** On green, factoryd starts a **reviewer** Assignment: a different session, a different account
   when two are available, a read-only worktree pinned to the head SHA. It reports `pass` or `changes_requested` with
   findings, which factoryd posts as a **comment by the reviewer bot**, whose hidden footer names the exact SHA and verdict (Gitea refuses
   an approval on your own PR, and a comment needs no special standing). `changes_requested` is a Trigger on the PR
   Thread → wakes the builder. At most **3 review rounds**; the 4th stops with a question to the owner. A round is one
   `changes_requested` plus its fix, so three negative reviews each get a fix and the 4th review is what asks. The owner's reply
   starts fresh rounds. A re-review caused only by a main update doesn't count.
   A reviewer reads the owner's later requests as well as the first one. If the owner speaks after a rejection and the head doesn't change, that
   head gets a fresh review (the owner can overrule a reviewer, but not skip the gate), and the overruled verdict stops counting. factoryd counts only what the forge shows
   from the reviewer bots on the exact SHA, so the reviewer's own report is never the pass. The Channel disarms any footer in text it
   posts, so a worker's words can't become a verdict.
6. **Gate 3 — current main.** If the head doesn't contain current `main`, factoryd asks Gitea to update the branch
   (`POST /pulls/<n>/update`); a conflict wakes the builder to merge main. Any new head SHA restarts gates 1–2. This holds for
   any move of `main`, not only one touching the PR's files: a failure we saw in production (§1.3) can come from files that
   don't overlap. A move after acceptance withdraws `ready` and runs this again.
   **Carried review (opt-in per repo).** A repo with `carry_review_across_main: true` in its
   policy keeps a reviewer's pass across a head that only took in `main`: when the review passed, factoryd recorded a
   fingerprint of the PR's diff against `main` (`pulls/<n>.diff` with `index` lines and hunk line numbers dropped, read only
   while the head stayed put); a later head with no review of its own and the **same** fingerprint counts as reviewed. CI
   still runs on the new head (Gate 1), and `main` must still be contained (this gate). Any real change to the diff, a
   conflict resolved by hand, or main changing lines right next to the PR's, makes a different fingerprint and a fresh
   review. A `changes_requested` on the head, or the owner asking for a fresh review, drops the carried pass. The ready comment
   says when a pass was carried and from which SHA. Off by default: every other repo keeps the rule above,
   a fresh review for every new head.
7. **Accepted.** All three gates pass on one SHA → the Ticket is `ready for the owner`. Merging is the owner's (or, if they later
   enable rule `merge-small`, a separate merge account under branch protection — open item O4).
8. **End.** The Ticket ends when its PR merges or closes, keeps its Subscriptions, and **resumes** if the owner comments on
   the issue or PR later — same worktree, same session, same Mandate. A stop from the owner (comment "stop", or a status-screen
   button) withdraws the Mandate for good.

Follow-ups: the owner's comment on a Ticket's Thread belongs to that Ticket's builder. The builder handles it if it fits the
scope, or hands it back (`handback`) with a reason; only then does it go to the Router as new work.

Questions: the builder asks through `report(blocked|waiting, question=…)` — one concrete question. factoryd posts it
where the owner made the request, and shows it at the top of the status screen. Already-authorized work continues meanwhile.

Turns that end badly never hang a Ticket silently. A turn ending with no usable report gets one reminder turn, then one question.
So does a claimed PR that isn't an open PR from the Ticket's branch into its base. A builder reporting `ready` on the same SHA that
CI failed or the reviewer rejected gets a question at once, not another loop.

Hold (`lumbridge-factory hold`, or a status-screen button): the running turn finishes, no new turn starts, gates take no action, and input queues until
release. Stop: kills the running turn and withdraws the Mandate; only an explicit new request (`/factory`) can make a new Ticket there.

---

## 6. Executors

One adapter interface, two implementations at launch.

```
start(ticket, role, worktree, prompt_parts, account, model) -> session_id
turn(session_id, inputs[]) -> stream of events, ending in a TurnResult
stop(session_id)
```

| | Claude Code (`/usr/bin/claude` pinned, 2.1.317 today) | Codex (0.163.0) |
|---|---|---|
| First turn | `claude -p --output-format stream-json --verbose --model <m> --permission-mode dontAsk --settings <ticket-settings.json> --mcp-config <ticket-mcp.json> --append-system-prompt-file <role.md> "<assignment>"` | `codex exec --json -s workspace-write -o <last.txt> "<assignment>"` |
| Next turn | same with `--resume <session_id>` | `codex exec resume <session_id> --json "<inputs>"` |
| Session id | from the `init`/`result` event | from the `--json` events |
| Policy hooks | `PreToolUse` hook in the ticket settings: refuses `git push` to `main`, `--force`, tags, `gh`/`tea` merge, reading `~/.config`, `/run/secrets`, other worktrees | Codex sandbox + the same deny list as an exec policy |
| Takeover by the owner | `claude --resume <id>` in a terminal on the host (an `attach` command that holds the Ticket meanwhile is planned) | `codex resume <id>` |

`start` prepares the worktree and config and runs no model, so `session_id` is the executor's handle for the Assignment. The agent
CLI's own id only exists once a turn has run; it comes back as `native_session_id`, and factoryd stores it and sends it on each turn, so
agentd holds no domain state. The wire protocol is `docs/PROTOCOL.md`; the shapes are `lumbridge_factory/core/executor.py`.

**Turns, not panes.** Each turn is a fresh process that exits when the model ends its turn. Inputs that arrive during a
turn are queued and delivered together as the next turn's message (batching). There is nothing to type
into, nothing to observe, and no identity to pin: the session id is in the output, and a resumed turn either works or
fails loudly. This removes Runner, tmux delivery receipts, `FACTORY_READY` bootstrap turns and hook UUID latching.

**Background work.** A turn has a wall-clock limit (default 45 min). Long commands (full test suites) run inside the
turn; a trick to move long commands to the background is not needed because nothing else waits on a turn.

**The worker's tools** (one stdio MCP server, `factory-tools`, started by the agent CLI as `<name>-factory`):

| Tool | Does |
|---|---|
| `report(status, summary, pr?, question?, verdict?, findings?)` | Writes the structured Report to the Assignment's inbox; factoryd validates it. A reviewer finishes with `ready` plus `verdict` (`pass` or `changes_requested`) and `findings` |
| `reply(purpose, text)` | Queues an acknowledgement or answer on the Ticket's own Thread (≤ 100 words in chat channels) |
| `follow(thread)` / `unrelated(thread)` | Subscribe for context only / drop a Thread the Router attached by mistake |
| `handback(trigger, reason)` | Give a follow-up back to the Router |
| `recall(id)` | Read a Trigger, Thread, Ticket or earlier Report by id (Chain of Recall, §10). Only what this Ticket may see. factoryd always connects to agentd, never the reverse, so the request travels as a turn event and the answer comes back with `answer_recall` |

The MCP server can only append to its Assignment's inbox. factoryd re-checks everything a Report claims (the PR exists,
its head is on the Ticket's branch, CI is what it says) before using it. Only a builder's `reply`, `follow`, `unrelated` and `handback`
count; a reviewer's only output is its verdict. `follow` is limited to the Ticket's own repo, and `handback` to a person's message this Ticket
actually received, excluding the request that created it.

---

## 7. Capacity and accounts

- **Slots are held only while a turn runs.** A Ticket waiting on CI, review or the owner holds nothing. Default 3 slots per
  instance.
- **One reserved slot for the owner**, beyond the 3. A turn caused by the owner's message takes a free slot, or this one when all are full.
  If that is also in use it goes first in the queue.
- Reviewer turns use the same pool and are scheduled before new builder launches, so acceptance never starves.
- Accounts: each turn asks the account router for one (`ports.AccountRouter`). The built-in `StaticAccounts` is the
  instance's list of credentials in order; one whose turn hits the provider's rate limit rests for 30 minutes. An add-on can
  supply its own router through `accounts_provider` in factoryd's config (`accounts.py` says how), and may report each
  account's use for the optional `tight_account` slot limit. Use only accounts you are entitled to use, under your model
  provider's terms. Builder default Opus 5.5, reviewer Sonnet 5.5 on a different account when both are under limit,
  Router Sonnet 5.5. Codex is built as an equal option per role and stays off by default.
- **Budgets are per Ticket in tokens and wall-clock**, set by the rule, and reaching one asks the owner instead of killing the
  work. Wall-clock counts time in turns, not time waiting. The running turn is never
  killed. The question comes only when another builder turn is needed, so a finished PR still gets its review and reaches `ready`. Session,
  worktree and queued input all stay. Any message from the owner answers it with one more budget from the current total; `stop` ends it.
- All accounts at their limit: turns wait in the queue, holding no slot. Nothing fails.

---

## 8. Reliability

- **Store first, act after.** Every outside effect — a comment, a PR review, a branch update, starting a turn — is an
  Outbox row written in the same transaction as the decision, and is performed after commit with retries
  (2 s doubling to 5 min). Each row has an idempotency key derived from its inputs, so a retry after a crash can check
  Gitea first ("is there already a comment with this key in its hidden footer?") instead of posting twice.
- **Restart = recovery note.** After factoryd, agentd or the host restarts, every Ticket whose turn was cut off gets a new
  turn on the same session with: *"The host restarted during your last turn. Check your worktree, `git log`, the
  branch on origin and the PR before repeating any outside action."* (after dsh-superagent's restart handling).
  The same session is kept and the turn's inputs are delivered again, because it may not have finished reading them. A turn still alive
  gets reattached with no note. One that finished while factoryd was down has its result collected. Five cut-offs in a row become one question, not a loop
  and not a quarantine. If a session can't resume, the Ticket gets a new Assignment in the same worktree with the original assignment again.
- **Quarantine is narrow:** only when an outside write's outcome is unknown and cannot be checked (none of ours, by
  design — every write is checkable on Gitea). The Ticket then shows one question to the owner.
- **Transient failures** (HTTP 408/429/5xx, connection reset, provider overload) retry 3 times with backoff before a
  turn counts as failed. Account-limit errors switch account through the router.
- **CI after a reboot:** factoryd reads job `completed_at` against the boot time; jobs killed by the outage are re-run
  (`POST …/jobs/<id>/rerun`) automatically, once.

---

## 9. Watchdog

Planned, not shipped in 0.1.0. A separate session (Sonnet 5.5, read-only tools + `hold`, `nudge`, `retry`, `notify`), run by a systemd timer every
10 minutes, outside factoryd:

1. **Deterministic checks first:** factoryd and agentd alive; every source polled in the last 2 minutes; no Outbox row
   failing 3 times; no turn past its limit; accounts under limit; disk on `/` above 15 %; egress unit active.
2. **Progress judgment** for each Ticket that has been `working` for more than 30 minutes: it reads the last Reports and
   the tail of the turn's events and answers *making progress / looping / drifted from the issue / stuck on the
   environment*. Anything but progress → `nudge` (a note into the next turn) once, then `hold` + one line to the owner.
3. It can be asked in plain words why something failed (`lumbridge-factory watchdog ask "…"`).

It never edits code, never accepts, and its own actions are Outbox rows like everything else.

---

## 10. Context: Inline & Spill, Chain of Recall

Every card the Router or an executor receives follows the same pattern:

- **Inline**: the object's id, title, author, the first 400 characters, and the ids of its neighbours (the comment it
  replies to, the PR's issue, the Ticket that owns the Thread).
- **Spill**: anything longer is truncated with `recall(<id>)` to read the rest. CI logs inline the failing step's last 60
  lines and spill the rest.
- Every recalled object inlines the ids of its own neighbours, so a session can walk from a comment to its Ticket to
  that Ticket's earlier review in three hops.

---


## 11. Code and operations

Python 3.12, standard library only, SQLite. The package is `lumbridge_factory/`:

```
lumbridge_factory/
  core/          domain: triggers, threads, evals, mandates, tickets, assignments, subscriptions (no I/O)
  engine.py      the one writer: core decisions -> one transaction -> Outbox
  store.py       SQLite schema, migrations, repositories
  effects.py     the Outbox (effect handlers are in engine.py)
  ports.py       Source, Channel, AccountRouter, Assessor interfaces
  turns.py       carries turn streams to the writer
  snapshot.py    the JSON a status screen reads (`lumbridge-factory --json status`)
  fakes.py       fake forge, executor, Sources, accounts, Router
  factoryd.py    the main loop: `python3 -m lumbridge_factory.factoryd --config <file>`
  gitea_api.py   a small stdlib client; never logs a token, follows no redirect
  accounts.py    the account router (StaticAccounts) and the add-on provider hook
  sources/       gitea.py
  channels/      gitea.py, feed.py
  gates.py       exact-SHA CI, reviewer, current-main
  router.py      the Router (no model yet)
  agentd/        endpoint daemon, executors (claude.py, codex.py), worktrees, policy hook, mcp_tools.py
  cli.py         `lumbridge-factory status|tickets|show|stop|hold|retry|policy|demo`
```

### 11.2 A real instance

A real instance needs, on one Linux host: three unix users (`<name>-control`, `<name>-factory`, `<name>-ci`), an egress
allowlist for the worker and CI users, a Gitea bot account that may comment and open PRs, a second bot for review comments,
branch protection on `main` (PR + required checks + a merge allowlist of people), a rootless Gitea Actions runner, an agent CLI
login per account, and systemd units for factoryd and agentd. There is no installer yet: `instances/sandbox/` is the only
example, and it is a sandbox (one shared account), not a production setup.

### 11.4 Upgrades

Releases are immutable directories with a receipt; switching the `current` link and restarting the units is a separate step.
A restart is safe at any time (§8).

## 12. Security

Learned from public critiques of [dsh-superagent](https://github.com/deepseek-ai/dsh-superagent-prototype) (workers with full access in observe mode, prompt injection,
budgets) and from our own history:

- Workers run as `<name>-factory`, not as the owner, behind egress. They cannot read factoryd's store or its Gitea tokens.
- The worker's git key can push only `factory/*` branches (Gitea branch protection on everything else), and it has no
  merge permission at all.
- Claude/Codex tokens are put in the turn's child environment only, never in a file in the worktree, and the PreToolUse
  hook refuses reads of `/run/secrets`, `~/.claude*`, `~/.codex*` and other worktrees.
- No real product data anywhere in the factory (§2 data rules).
- Untrusted text is quoted inside a fenced card with its author, never pasted into a system prompt.
- Every proactive rule starts in `observe`.

---

