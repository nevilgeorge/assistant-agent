# Running agent CLIs inside Docker

Last updated: 2026-10-06.

This living note describes the implemented transport, conversation lifecycle, recovery,
and storage boundaries. Conversation transcripts and state remain only in app memory.

## Phase 2: Dedicated conversation sandbox lifecycle

Each authenticated internal user shares one conversation across browser tabs and logins.
The first accepted prompt reserves capacity and creates a dedicated container, represented
by an immutable handle with container ID, internal user ID, conversation ID, and host/app
input paths. Later turns reuse that assignment and its persistent Claude process. The
handle belongs to the conversation independently of whether Claude attached successfully.

The app owns container creation, readiness, retirement, and recovery. Compose owns only
the app, PostgreSQL, Caddy, and networks. Allocation/readiness has a 30-second budget;
Claude startup then has its existing 15-second budget. Images must already exist, and
request handling never pulls them. Explicit container IDs are required for exec and
readiness; there is no shared-container fallback.

Retirement closes the process group and exec attachment, force-removes the assigned
container even if process cleanup fails, then deletes input directories. Reset,
disconnect, idle expiry, turn/transport failure, and app shutdown use this path. Allocation
and process startup both check retirement before activating their acquired resources.
Accepted startup and cleanup survive HTTP request cancellation. Failed destruction stays
queued for sweeper retries and continues to occupy capacity until removal is confirmed.
Account disconnection deletes credentials and browser sessions even if Docker is down.

Startup reconciles containers with application ownership and this deployment's labels,
removes earlier assignments, then cleans abandoned generated input directories. It never
adopts an old container because conversations are not persisted. Deterministic names
allow ambiguous create results to be recovered. Another deployment's containers and
unrelated files are untouched. Generated-directory cleanup rejects symlinks and escaping
paths. Inputs are deleted only after the corresponding container is gone. If Docker is
unavailable or cleanup is incomplete, the web app stays available while new sandbox
allocation is blocked; reconciliation retries before agent startup.

Run one app worker/replica per deployment. `CHAT_MAX_SESSIONS=4` counts pending allocation,
live containers, and failed container cleanup; reset/startup races must not exceed that
cap. Four slots accept memory overcommit on the current 4 GiB production host.

## Storage and security

`SANDBOX_IMAGE`, `SANDBOX_DEPLOYMENT_ID`, `SANDBOX_NETWORK`,
`SANDBOX_HOST_INPUT_ROOT`, and `SANDBOX_APP_INPUT_ROOT` configure lifecycle.
Production's daemon-host root is `/srv/assistant-agent/session-inputs`. Local deployment
resolves `<repo>/data/session-inputs` to an absolute path. Both mount the root read/write into
the app at `/srv/assistant-agent/session-inputs`; bind sources for sandboxes use the host root.

The root belongs to app UID 10001 and uses `0700`. Each server-generated
`session-inputs/<conversation-id>` directory belongs to the app and uses `0755`,
bound as `/input:ro`.
Future atomic publication must use `0644` files and `0755` nested directories for sandbox
UID 1000. File downloads and publication belong to later phases. These directories are
inputs, not persisted conversations. `/workspace` remains in the image's writable
agent-owned layer; removal discards workspace files, copied inputs, processes, and CLI
state. Old shared workspace files are left unused.

Containers preserve non-root execution, init, dropped capabilities,
`no-new-privileges`, PID limit 2048, 2 GiB memory, one CPU, and bounded Docker logs. They
have restart policy `no`, publish no ports, and mount no Docker socket. Sandboxes join
only the explicitly named bridge; the app has alias `app`. Backend services stay on the
existing Compose network. The bridge exposes all app routes and is not complete network
isolation; egress remains unrestricted.

Google/application credentials never enter container-wide sandbox environment variables.
The Anthropic key is forwarded through aiodocker's exec environment, never interpolated
into shell commands or logged. Separate control and chat Docker clients are retained.
The app's Docker socket still grants root-equivalent control over the host.

## Transport and browser behavior

The app keeps a non-TTY Docker exec open as sandbox user `agent` in `/workspace` and sends
newline-delimited stream-json prompts to its stdin. Claude runs under `setsid --wait`
with a PID file for process-group and descendant termination. Claude keeps context across
turns, uses `--no-session-persistence`, and has tools and MCP disabled. Token issuance,
Gmail/MCP activation, file-download tools, and transcript persistence are later phases.

A background reader drains separate stdout/stderr, decodes UTF-8 incrementally, and
normalizes Claude JSON into bounded transcript/events. Stderr and raw prompt diagnostics
are not logged. `POST /api/message` requires the authenticated cookie and CSRF token,
returns `202` with conversation/turn IDs, and rejects overlapping prompts with `409`.
`GET /api/conversation` provides the in-memory snapshot. SSE at
`GET /api/conversation/stream?conversation_id=...&after=...` streams deltas and turn events,
with sequence replay and `Last-Event-ID` support. Stale cursors trigger snapshot reload.
`POST /api/conversation/reset` retires the assignment. The browser renders plain text.

Browser disconnection does not stop an accepted turn. Turns retain the 120-second
budget; idle expiry defaults to 1,800 seconds. Conversations permit 2 MiB cumulative
prompt/response text and replay the last 2,048 events. A failed CLI conversation needs a
new conversation. App restart loses transcripts and Claude context; there is no automatic
retry or resume.

## Deployment and diagnostics

Local deployment builds a tagged sandbox image and initializes app-owned storage using
one root app container. Production explicitly pulls the existing sandbox image and
initializes session storage on the retained volume. Both remove the retired Compose
sandbox as an orphan. The byte-identical vendored image is unchanged.

```bash
uv run assistant-agent sandbox
uv run assistant-agent sandbox --container CONTAINER_ID claude --version
SANDBOX_DOCKER_INTEGRATION=1 uv run pytest -q tests/test_sandbox_async.py
```

Untargeted status inspects daemon/image/network readiness without creating a sandbox.
Unit/web tests use injected lifecycle/process fakes; opt-in Docker checks use a small
streaming process without model calls. An optional real Claude check remains separate:

```bash
CHAT_DOCKER_INTEGRATION=1 uv run pytest -q tests/test_chat.py -k real_claude
```
