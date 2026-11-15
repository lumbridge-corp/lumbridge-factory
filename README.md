# Lumbridge Factory

An engine that takes issues, drives a coding agent in a sandbox, opens pull requests and repairs failed checks; people merge.
It is for a small team that wants agents to do the routine part of its issue queue on its own Gitea server. What makes it
different: acceptance is checked by the host against the forge, on the exact commit, never taken from an agent's word.

Part of [Lumbridge](https://lumbridgecorp.com) ([all repositories](https://github.com/lumbridge-corp)). In daily use.

## Status

- **Version:** 0.1.0. Interfaces can change until 1.0.
- **Runs on:** Linux, Python 3.12 or newer, standard library only.
- **Limits, first:**
  - **Gitea only.** It does not work with GitHub or GitLab yet: the Source, the Channel and the gates speak Gitea's API.
  - **One agent CLI.** Workers run Claude Code. A Codex executor is built and off by default.
  - **No installer.** A real instance is host set-up you do by hand (users, egress, a runner, units): see
    [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §11.2. `instances/sandbox/` is the only example, and it is a sandbox.
- **Not yet:**
  - A packaged release or download.
  - Other forges.
  - The model Router (`router.py` decides in code only) and the watchdog session.

## Install

You need Python 3.12 or newer and git. Nothing else for the engine and its tests.

On Ubuntu 24.04:

```sh
sudo apt install python3 git
git clone https://github.com/lumbridge-corp/lumbridge-factory.git
cd lumbridge-factory
python3 -m unittest discover -s tests
```

`bin/lumbridge-factory` runs the CLI from the checkout. `pip install .` installs the same command as `lumbridge-factory`.
The CLI writes only the SQLite store you name with `--db` (default `./factory.sqlite3`). Remove the checkout and the store
to uninstall.

## Quick start

```sh
bin/lumbridge-factory --db scratch.sqlite3 demo
bin/lumbridge-factory --db scratch.sqlite3 status
bin/lumbridge-factory --db scratch.sqlite3 tickets
bin/lumbridge-factory --db scratch.sqlite3 --json status
```

`demo` fills a scratch store with fake issues, a fake forge and a fake agent, and runs them through the engine. `status` shows
what needs you and what is working; `--json status` is the snapshot a status screen reads.

A real instance needs a Gitea server you run, two bot accounts, a rootless Gitea Actions runner, Claude Code with its own
login, and network sandboxing for the worker. `python3 -m lumbridge_factory.factoryd --config <file>` runs the engine and
`python3 -m lumbridge_factory.agentd --config <file>` the agent daemon; `instances/sandbox/` shows both config files.

## Configuration

| What | Where |
|---|---|
| Engine config | `factoryd.json`, passed with `--config` (example: `instances/sandbox/factoryd.json`) |
| Agent daemon config | `agentd.json`, passed with `--config` (example: `instances/sandbox/agentd.json`; reference: [docs/AGENTD.md](docs/AGENTD.md)) |
| Store | the SQLite file named by `db` in `factoryd.json` |
| Accounts | `accounts` in `factoryd.json`, tried in order; `accounts_provider` names an add-on's router instead ([lumbridge_factory/accounts.py](lumbridge_factory/accounts.py)) |

The policy (who may ask for work, repositories, data rules, slots) is installed from `factoryd.json` on a new store; after
that, change it with `lumbridge-factory policy ...`.

## How it fits the system

It works alone: it needs a Gitea server and Claude Code, and no other Lumbridge part. Lumbridge's own repositories are built by
it.

| Works with | What passes between them |
|---|---|
| Gitea | issues, labels, comments, pull requests, commit statuses, over Gitea's HTTP API and SSH |
| Claude Code | one `claude -p` process per turn, with a policy hook and the factory's MCP tools |

## What it sends and where

factoryd talks to your Gitea server only. agentd starts Claude Code, which talks to Anthropic with the account you give it, and
pushes `factory/*` branches to your Gitea over SSH. Issue and PR text, CI log tails and the repository's code go to the model
as part of each turn. The store, worktrees and session files stay on the host. Nothing is sent anywhere else.

## Docs

- [Architecture and threat model](docs/ARCHITECTURE.md): the domain, a ticket's life, the gates, capacity, reliability, security
- [agentd](docs/AGENTD.md): the agent daemon, its config and its limits
- [Protocol](docs/PROTOCOL.md): the wire contract between factoryd and agentd
- [Change log](CHANGELOG.md)

## Contributing

Bug reports and small fixes are welcome. Talk to us in an issue before a large change.
See [CONTRIBUTING.md](CONTRIBUTING.md) for how to build, test and send a pull request.

```sh
scripts/scrub-check
python3 -m unittest discover -s tests
```

## Security

Do not open a public issue for a security problem.
Email security@lumbridgecorp.com or use "Report a vulnerability" on this repo's Security tab.
See [SECURITY.md](SECURITY.md) and the threat model in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §12.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
