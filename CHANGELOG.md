# Changelog

Lumbridge Factory.

## [Unreleased]

## [0.1.0] - 2026-11-13

The first public release.

### New
- **The engine** (`factoryd`): Gitea Source and Channel, explicit requests by label or `/factory`, tickets with their own
  branch, PR and worktree, the three gates (exact-SHA CI, a separate reviewer, current `main`), the Outbox with idempotent
  writes, recovery after restarts, slots by turn and per-ticket budgets.
- **The agent daemon** (`agentd`): one Claude Code process per turn, resumable sessions, a PreToolUse policy hook, the
  `factory-tools` MCP server, worktrees and a scrubbed child environment. A Codex executor, off by default.
- **The CLI** (`lumbridge-factory`): `status`, `tickets`, `show`, `stop`, `hold`, `retry`, `policy`, `demo`.
- **Accounts**: each turn takes an account from a router. The built-in one is a fixed list where an account that hits a
  rate limit rests; `accounts_provider` in `factoryd.json` loads another router from an add-on package.
- `instances/sandbox/`: an example instance on one shared account, for trying it on a throwaway repository.

### Changed
- The package is `lumbridge_factory` and the command `lumbridge-factory`. The footer on the factory's forge comments and the
  snapshot's needs key can be set with `LUMBRIDGE_FACTORY_TAG` and `LUMBRIDGE_FACTORY_NEEDS_KEY` (defaults `lumbridge-factory`
  and `needs_owner`).
