# agentd

The endpoint daemon of ARCHITECTURE §3 and §6. It starts and stops agent turns, prepares worktrees and returns output. The wire
contract is [PROTOCOL.md](PROTOCOL.md); this file covers what the contract doesn't.

Run it with `python3 -m lumbridge_factory.agentd --config <file>`. Python 3.12, stdlib only. factoryd uses
`lumbridge_factory.agentd.client.AgentdClient`, which implements `core.executor.Executor`.

| File | Does |
|---|---|
| `daemon.py`, `client.py` | Both ends of the socket |
| `turns.py` | Sessions, the turn journal, process supervision, restart recovery |
| `claude.py`, `codex.py` | Each tool's argv, stream parser and outcome classifier |
| `worktrees.py` | One bare clone per repo, one worktree per name, pre-trust |
| `session_files.py` | Per-ticket `settings.json`, `policy.json`, `role.md`; per-turn `mcp.json`. All outside the worktree |
| `childenv.py`, `scrub_shell.sh` | The cleared child environment; tokens |
| `hook.py` | The PreToolUse policy. Self-contained, runs under `python3 -I` |
| `mcp_tools.py` | The `factory-tools` MCP server. Self-contained, runs under `python3 -I` |
| `procs.py` | Finding and killing what a turn left behind |

## Configuration

```json
{
  "socket": "/run/acme-factory/agentd.sock",
  "socket_group": "acme-control",
  "allowed_peer_uids": [967],
  "state_dir": "/var/lib/acme-factory/agentd",
  "worktrees_dir": "/var/lib/acme-factory/worktrees",
  "repos_dir": "/var/lib/acme-factory/repos",
  "agent_home": "/var/lib/acme-factory",
  "repos": {"acme/widgets": "ssh://git@git.example.com:2222/acme/widgets.git"},
  "tools": {"claude": {"enabled": true, "bin": "/usr/bin/claude", "expected_version": "2.1.317"}},
  "accounts": {"claude-a": {"tool": "claude", "env_file": "/run/secrets/acme-factory.env", "env_var": "CLAUDE_CODE_OAUTH_TOKEN_A"}},
  "child_env": {"GIT_SSH_COMMAND": "ssh -i ... -o IdentitiesOnly=yes", "GIT_AUTHOR_NAME": "...", "GIT_AUTHOR_EMAIL": "..."}
}
```

The values above are examples, not recommendations for a real install. Config refuses a token value. `child_env` refuses credential-like names,
`HOME` and `PATH`. Codex stays off unless `tools.codex.enabled` is true.

## How a turn stops

Every outcome ends in a stored `TurnResult`:

- **Stop or timeout:** SIGTERM to the process group, SIGKILL after `kill_grace_seconds`.
- **The model finished but the process doesn't exit:** killed after `post_result_grace_seconds`, and still `completed`.
- **Any end:** the group is swept, then `/proc` is scanned for `LUMBRIDGE_FACTORY_TURN=<turn_id>`, which catches what left the group with
  `setsid`. Background jobs don't outlive their turn.
- **agentd gets SIGTERM:** running turns end `interrupted`, with results written.
- **agentd gets SIGKILL:** turns keep running until the next start, which kills them by marker and stores `interrupted`, keeping
  their reports and native session id.

## Decisions made while building

These are the gaps the design left open, found while building. Each takes the simplest answer that fits the design.

1. **`mcp.json` is per turn, not per ticket.** It names that turn's inbox. `settings.json`, `policy.json` and `role.md` are written
   at `start`, as PROTOCOL.md says.
2. **Project settings are ignored (`--setting-sources ""`).** A repo or a worker could otherwise put `disableAllHooks` in
   `.claude/settings.local.json`. Measured: with `disableAllHooks` committed in both project settings files, the hook still fired.
   `.mcp.json` is ignored through `--strict-mcp-config`. The cost, also measured: the repo's `CLAUDE.md` is no longer loaded, so `role.md` tells the builder to
   read `AGENTS.md`/`CLAUDE.md` first. §4.3's rank 4 is therefore a request rather than a guarantee.
3. **Not `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`, though it's the obvious switch.** In 2.1.310 it forces permission mode `default`, makes
   Claude's sandbox mandatory (every Bash call failed: no socat) and creates empty stubs in the worktree, including `package.json`,
   `.env` and `.npmrc`, which would be committed by a careless `git add -A`. Instead `CLAUDE_CODE_SHELL_PREFIX` points at
   `scrub_shell.sh`, which unsets the credentials before every Bash call. Measured: the variable is gone from a Bash child.
4. **The wrapper is a fail-open risk.** Claude runs hooks through the prefix, and a hook that exits 127 doesn't block. agentd refuses
   to run Claude if the wrapper or hook is missing or not executable. A release must keep the wrapper's execute bit.
5. **The tool comes from the account.** `StartRequest` has no tool field, so the account's `tool` decides. A session can't move between
   Claude and Codex, because neither can resume the other's native id. To switch tools, factoryd makes a new Assignment.
6. **Tokens** are read from the account's env file at each turn, so a rotation needs no restart. A token the agent prints is
   redacted from events, logs and inbox entries as agentd reads them. `inbox.jsonl` itself keeps what the tool wrote until `forget`.
7. **Reviewer worktrees** are chmod read-only as well as refused by the hook. Tests that write into the tree will fail in a review.
8. **Session stores** set `cleanupPeriodDays` high so a Ticket can resume long after it ended (§5.8).

## What the hook can't stop

The hook is defense in depth. It reads a command's text before the shell does, as the same unix user as the agent. **The git key and
Gitea branch protection are what stop a push to `main`, and every install must verify them independently.** The hook refuses the honest mistake
and the obvious attempt: `tests/test_agentd_hook.py` has about 380 hostile and everyday cases. It can't stop:

- **Code that runs but isn't on the command line.** `python3 x.py`, `npm test`, `make`, a test suite, a git hook, or a repo's
  `Makefile` can push or read anything the unix user can. Only shell scripts it's asked to run directly get scanned. Measured:
  `python3 check_env.py` ran and read `/proc`.
- **Text assembled at runtime.** `open('/run/sec' + 'rets/x')`, `chr()` tricks, or `a=$(printf gi; printf t); $a status` piped through
  an interpreter.
- **The token, from a determined worker.** Measured: a Bash child doesn't have it, but `/proc/<claude>/environ` is readable by the
  same uid and does hold it. Only the literal path in a command is refused. The env file is also readable to that uid. See below.
- **Other tickets.** All turns share one uid, so one worker can signal or read another's files once it gets past the text check.
- **Exfiltration of anything it can read.** Only egress limits that, and Anthropic's API is on the allowlist by necessity.
- **Forged inbox entries.** A worker can append to its inbox past the hook. PROTOCOL.md already treats entries as claims.
- **Escaped processes.** A background job that does `setsid` *and* clears its environment survives its turn, until the unit stops.

False refusals exist too: a command that merely mentions `git push` or `/run/secrets` in an argument to a non-search command is
refused. The reason goes to the model, which is told to report `blocked` rather than work around it.

Codex has less: prefix rules cover the command shapes in `codex.py`, can't express "only this branch", and don't cover reads at all.
Its event names are from documentation, unverified against a real turn. Run one before enabling it.

## For factoryd

- `AgentdClient.turn` raises `ExecutorError("unavailable")` if the connection drops. Call `turn` again with the same `turn_id` to
  attach, or `collect` after a restart. A new `turn_id` while one is running gets a retryable `unavailable`.
- `running()` then `collect()` after any restart. Call `forget` after storing a result, or turn directories accumulate.
- Transient and account-limit outcomes come from text in the CLI's output. The patterns in `claude.py` have only partly met real
  errors: `resume_failed` has, the rest are from known messages. Treat `failed` as possibly misclassified.
- `remove_session` deletes a session and optionally its worktree (an earlier gap, now closed). The engine's `clean_up` drives it: retired
  sessions at once, an ended Ticket's worktree after `retain_ended_seconds`. A shared bare clone per repo stays.
- `tokens` counts input, cache creation and output, not cache reads. On a resumed session cache reads dominate, so budgets measure work.

## Before a real install

- Set `allowed_peer_uids` to `<name>-control` only. Workers share agentd's uid, so without this a worker that gets past the hook can
  drive the socket.
- Deliver tokens so the worker's uid can't read the file: a root-only `EnvironmentFile=` on the unit and `env_var` with no `env_file`.
  agentd makes itself non-dumpable, so its `/proc` environ isn't readable by workers. This narrows the exposure and doesn't close it:
  the running CLI's own environ stays readable.
- `KillMode=control-group` on the unit, so stopping it catches anything that escaped a turn. `TimeoutStopSec` above
  `kill_grace_seconds` + 20.
- Verified on 2.1.310. On a newer CLI, re-run one turn and re-check: hook fires, `scrub_shell.sh` removes the token,
  no stub files appear, `--setting-sources ""` still parses, and hostile project settings are still ignored.
- Set `expected_version`. Check the wrapper's execute bit survives release install. `worktrees_dir` must exist or be creatable.
- Test the git key and branch protection directly. They are the real boundary.

## Evidence

Real turns, 2026-11-04, Sonnet, Claude Code 2.1.310,
scratch `git init` repos with a bare local origin. Six turns on one session:

| Turn | Result |
|---|---|
| t1 | `ready` through the tool. Project `CLAUDE.md` code word not seen |
| t2 | `--resume` recalled the word from t1 |
| t3 | The hook refused `git push origin HEAD:main`, `--force-with-lease` and `cat ~/.claude.json`. Other Bash calls failed on the scrub variable's sandbox, which is why it was dropped |
| t3b | `main` refused again; `git push origin HEAD:factory/real` worked; `origin` shows `main` unchanged at `dae685c` |
| t4 | `stop` gave `stopped` in 6 s with no processes left. The CLI had itself refused the foreground `sleep 300`, so this didn't kill a running command. The fake-CLI tests prove that part |
| t5 | Resumed after the stop and still recalled the word |

No file the run wrote holds the token (213 scanned). A resume of a nonexistent id, with no model call, exits 1 with
`No conversation found with session ID`, which classifies as `resume_failed`.
