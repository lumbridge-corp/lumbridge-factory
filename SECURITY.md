# Security

Report security bugs by email to security@lumbridgecorp.com, or with "Report a vulnerability" on this repo's Security tab.
Do not open a public issue with exploit details.

Include the commit you run, your OS, your Gitea and Claude Code versions, steps to reproduce, and the impact you expect.
Remove tokens, account files and private repository content from logs and examples. A small reproduction helps.

The latest `main` is supported. We answer in the same thread. There is no fixed response deadline.

## Trust model

The factory runs coding agents with write access to branches of your repositories, so read
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §2, §3 and §12 before you run a real instance. In short:

- Work starts only when a person on the instance's allowed list asks for it (a label or `/factory`). Issue bodies, comments, CI
  logs and documents are data for the agent, never permission.
- Acceptance is read back from Gitea on the exact head commit: required CI green, a pass from a separate reviewer account, and
  current `main` contained. An agent's report is never the pass.
- Workers run as their own unix user behind an egress allowlist, hold no forge token, and push only `factory/*` branches; branch
  protection is what stops anything else. The policy hook (`lumbridge_factory/agentd/hook.py`) refuses pushes to `main`, force
  pushes, tags, merges and reads of secret locations, but it is a second line: the git key's permissions and branch protection
  are the real boundary.
- Agent account tokens live only in the turn's child environment.

In scope: a way for outside text to make the factory start, approve, merge or push something the policy does not allow; a way
for a worker to reach the store, the forge tokens, another ticket's worktree or a protected branch; a way to make the factory
post as its reviewer. Out of scope: what a person on the allowed list asks for on purpose, and the agent CLI's own behaviour
outside the factory.
