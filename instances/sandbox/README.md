# Example instance: a sandbox

Config for trying the factory on one throwaway repository named `factory-sandbox` (any owner; the policy refuses every other
name). One Gitea account plays the person, the factory and the reviewer, so the factory tells its own comments apart only by
their footer. That is fine for a trial and wrong for real work: a real instance has separate bot and reviewer accounts and
runs workers as their own unix user behind an egress allowlist (docs/ARCHITECTURE.md §3, §11.2, §12).

- `factoryd.json`: the engine. Set `gitea_url`, the token files, the account names and the repository.
- `agentd.json`: the agent daemon. Set the clone URL, the Claude Code path and version, and the account's token file.
- `ci_standin.py`: NOT real CI. It plays a runner for the sandbox repository, so you can try the loop without a Gitea runner.

```sh
python3 -m lumbridge_factory.agentd --config instances/sandbox/agentd.json
python3 -m lumbridge_factory.factoryd --config instances/sandbox/factoryd.json
```
