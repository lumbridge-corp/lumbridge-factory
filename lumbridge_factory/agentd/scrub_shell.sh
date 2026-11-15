#!/bin/sh
# CLAUDE_CODE_SHELL_PREFIX: Claude Code runs every Bash tool call and hook as
# `scrub_shell.sh '<command>'`. The agent's credentials stay out of the environment of whatever it
# runs (not out of /proc/<claude>/environ: see docs/AGENTD.md, "What the hook can't stop").
#
# Not CLAUDE_CODE_SUBPROCESS_ENV_SCRUB: in 2.1.310 that also forces the permission mode
# to default and requires Claude's sandbox, so every Bash call fails without socat.
unset CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CODE_MESSAGING_TOKEN CLAUDE_CODE_SESSION_ACCESS_TOKEN ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN OPENAI_API_KEY CODEX_API_KEY
exec /bin/bash -c "$1"
