## Assistant Agent
This is an application that orchestrates personal assistant agents for users:
1. Users authenticate with Google so that the application can read from mail/calendar. 
2. The app retrieves mail and calendar events from the past 6 months and indexes them. 
3. The app orchestrates worker agents in sandboxes that are given these files to perform operations.  

**Important note**: Agents can be run in sub-folders that have their own CLAUDE.md files. This file is specific to developing this application; it does not contain guidelines on how to run personal assistant agents.

## General guidelines
Use subagents liberally (parallelize work, side exploration that shouldn't pollute the main context, etc).

Never edit `data/*/CLAUDE.md` or `data/*/build_index.py` directly. Those are generated
copies; the tracked source is `src/assistant_agent/agent_kit/`. Edit it there and run
`uv run assistant-agent install-kit --force`. `install-kit --check` reports copies that
have drifted.

`src/assistant_agent/sandbox_kit/` is different despite the similar name: it is a
byte-identical vendored copy of the `agent-sandbox` build context, not an installed kit.
Nothing copies it anywhere and `install-kit` ignores it — `deploy/deploy.sh` builds it from
the working tree. Change it upstream in `agent-sandbox` and re-sync; see its `UPSTREAM.md`.
Never put a provenance header on that `Dockerfile`: line 1 must stay
`# syntax=docker/dockerfile:1` or BuildKit's frontend selection silently turns off.

## Writing Python
- Use type hints extensively
- Use ruff for linting
