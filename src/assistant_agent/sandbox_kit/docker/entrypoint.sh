#!/usr/bin/env bash
set -euo pipefail

# Runs as `agent` (set by the Dockerfile); nothing here needs root.

# Bind-mounted repos come from another machine's uid namespace, so git treats
# them as having dubious ownership unless we opt out.
git config --global --add safe.directory '*' 2>/dev/null || true

# Seed an identity from the host's git config, if the wrapper passed one along
# and the user has not already configured one inside the container.
if [ -n "${GIT_USER_NAME:-}" ] && [ -z "$(git config --global --get user.name || true)" ]; then
    git config --global user.name "$GIT_USER_NAME"
fi
if [ -n "${GIT_USER_EMAIL:-}" ] && [ -z "$(git config --global --get user.email || true)" ]; then
    git config --global user.email "$GIT_USER_EMAIL"
fi

# Codex authenticates from ~/.codex/auth.json, not from the environment. Its
# websocket transport (wss://api.openai.com/v1/responses) sends no Authorization
# header without that file, so every call 401s with "Missing bearer or basic
# authentication in header" even though OPENAI_API_KEY is set and valid --
# `codex doctor` reports `auth mode: none`. The container is stateless, so seed
# the file on every start. Claude Code reads ANTHROPIC_API_KEY directly and
# needs no equivalent.
if [ -n "${OPENAI_API_KEY:-}" ] && [ ! -f "$HOME/.codex/auth.json" ]; then
    if ! printenv OPENAI_API_KEY | codex login --with-api-key >/dev/null 2>&1; then
        printf 'entrypoint: codex login --with-api-key failed; codex will not authenticate.\n' >&2
    fi
fi

exec "$@"
