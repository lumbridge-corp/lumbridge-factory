# Contributing

Lumbridge is at v0.1 and maintained by a small team, so please read this first.

- **Bugs:** open an issue. Say what you did, what happened, what you expected, your OS, and the commit.
- **Security problems:** do not open an issue. See [SECURITY.md](SECURITY.md).
- **Small fixes** (typos, clear bugs, docs): send a pull request.
- **Larger changes** (a new forge, a new executor, changes to the store or the protocol): open an issue first and wait for a
  reply. It is better to hear "no" before you write the code.

## Build and test

Python 3.12 or newer, standard library only. What CI runs:

```sh
scripts/scrub-check
python3 -m unittest discover -s tests
```

Tests use a fake forge, fake agents and temporary folders; they never reach a real Gitea or a model. Keep it that way: no test
may need a network, an account or a real repository.

## What we look for

- One change per pull request, with tests for behaviour you change or add.
- Docs (`docs/ARCHITECTURE.md`, `docs/AGENTD.md`, `docs/PROTOCOL.md`) updated in the same pull request when behaviour changes.
- A line in `CHANGELOG.md` under "Unreleased" for anything a user would notice.
- Standard library only. A new dependency needs a strong reason in the pull request.
- No new network calls beyond the forge and the agent CLI the instance configures.

## Never commit

Passwords, tokens, keys, account files, real `.env` files, real names or addresses of people, private host names or network
addresses. Use made-up data (`example.com`, `acme/widgets`). The scrub check in CI fails the build on these; if it flags
something that is fine, say why in the pull request.

## Commits and pull requests

Write the commit subject as a short sentence that says what changed. Say in the pull request what the change does, why, and
how you tested it. Say so if any part is copied or adapted from elsewhere, with its source and licence. A maintainer reviews
and merges.

By sending a contribution you agree that it is licensed under the repository's licence, Apache-2.0.
