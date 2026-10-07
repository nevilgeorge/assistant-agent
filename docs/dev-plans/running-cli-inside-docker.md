# Running agent CLIs inside Docker

Last updated: 2026-10-07. Phases 1–5 are implemented. Phase 6 validation is in
progress; the evidence checklist below remains pending until the checks pass.
This runbook describes the current local architecture. Production deployment is
a separate action and is outside this validation work.

## Assignment, startup, and cleanup

Each authenticated internal user shares one in-memory conversation across browser
tabs and logins. The first accepted prompt reserves capacity and allocates a
dedicated container. Its immutable assignment records the container, internal user,
conversation, and host/app input paths. Later turns reuse the container and Claude
process. Allocation has a separate 30-second deadline; request handling never
pulls an image and never falls back to a shared container.

After allocation, the app issues a fixed 24-hour conversation grant and provisions
root-owned `/run/assistant/mcp.json` through Docker's archive API. The file contains
a literal `${ASSISTANT_MCP_TOKEN}` placeholder and `http://app:8000/mcp/gmail`;
it contains no credential. Its directory is 0755 and file is 0644, so `agent` can
read them but cannot replace them. The raw grant and Anthropic key enter only the
Claude Docker exec environment. They are absent from container-wide configuration,
launch command arguments, conversation state, and routine logs.

The output reader starts immediately after attachment. Request-ID-correlated
`initialize` and `mcp_status` control requests use serialized stdin writes. Gmail
is ready only after successful initialization and a connected `gmail` server
advertising all five expected tools. No model prompt or `system/init` event is
needed to establish discovery. Control responses stay inside `ClaudeProcess`;
complete responses may include authentication headers and must never be logged.
Discovery polls every 250 ms for at most 10 seconds (`MCP_TIMEOUT=10000`). Each
process launch has a 15-second limit; provisioning, first launch, bounded cleanup,
and fallback share a 45-second budget after grant issuance.

A Gmail configuration/discovery failure stops the provisional process, revokes
its grant, and retires/drains download work before one chat-only replacement
launches in the same container. The replacement receives strict empty MCP
configuration, blanket MCP denial, no Gmail token, and unavailable guidance.
The first prompt goes to the selected final process exactly once. Subsequent
turns remain available; the browser persistently shows “Gmail is unavailable in
this conversation. Reset to retry.” Reset creates a new conversation and retries
Gmail. No grant is reissued for the degraded conversation, and its original
expiry still bounds its lifetime. Grant issuance, transport/protocol, failed
termination, and replacement-launch failures retain normal conversation failure.

Reset, disconnect, inactivity, grant expiry, turn/transport failure, and shutdown
retire the assignment. Retirement denies authorization immediately, revokes the
grant, drains file work, stops the process group and exec attachment, removes the
container, and only then deletes inputs. Accepted startup and cleanup survive HTTP
request cancellation. Failed destruction remains queued for sweeper retries and
continues to occupy capacity until removal is confirmed. Credential/session
removal on disconnect does not depend on Docker being available.

Startup invalidates outstanding grants and reconciles containers with this app's
ownership/deployment labels, then removes abandoned generated input directories.
It never adopts a previous container because transcripts and Claude context are
not persisted. Other deployments and unrelated files are untouched. Cleanup
rejects symlinks and escaping paths. If Docker is unavailable or cleanup is
incomplete, the web app remains available while new allocations are blocked.
Reconciliation retries before subsequent agent startup.

## Configuration and startup order

Run **one app worker/replica per deployment, with one deployment per database**.
Assignments, replay state, and capacity are in memory. Multiple workers cannot
share their authorization or cleanup state safely.

| Setting | Mapping and purpose |
| --- | --- |
| `DATABASE_URL` | App database containing OAuth credentials, browser sessions, and hashed conversation grants. |
| `CREDENTIAL_ENCRYPTION_KEY` | App-side encryption key for stored Google credentials; never forwarded to sandboxes. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | App-side OAuth client configuration. |
| `APP_ENV`, `BASE_URL` | OAuth/browser origin; production requires HTTPS. The internal MCP address remains `app:8000`. |
| `ANTHROPIC_API_KEY` | Model credential injected into the Claude exec environment. |
| `SANDBOX_IMAGE` | Existing built sandbox image; default `assistant-agent-sandbox:local`. Validation pins its resolved image ID. |
| `SANDBOX_DEPLOYMENT_ID` | Safe ownership label/name component; default `local`. Keep stable for recovery. |
| `SANDBOX_NETWORK` | Explicit sandbox bridge; default `assistant-agent-local-sandboxes`. App joins with alias `app`. |
| `SANDBOX_HOST_INPUT_ROOT` | Absolute directory as seen by the Docker daemon, used as sandbox bind source. Local script resolves `<repo>/data/session-inputs`; production uses `/srv/assistant-agent/session-inputs`. |
| `SANDBOX_APP_INPUT_ROOT` | The same storage as seen by the app. Compose mounts it at `/srv/assistant-agent/session-inputs`. For direct host execution, both roots normally match. |
| `CHAT_MAX_SESSIONS` | Default 4; counts pending allocation, live containers, and failed removal. |
| `CHAT_IDLE_SECONDS` | Default 1800 seconds; grant expiry remains a separate fixed maximum. |

Gmail concurrency, quota, request-size, and file-size limits are constructor
settings in `GmailLimits` and session/MCP services, not additional environment
variables. Discovery and startup budgets are code constants. Do not invent
configuration knobs when investigating a timeout.

For a new or upgraded local app, the existing startup order is:

1. Make the selected app/sandbox images available and create the configured Docker
   network and input storage. The input root belongs to app UID 10001 with mode
   0700; the app needs access to the Docker socket's group.
2. Start PostgreSQL and wait for health.
3. Run `alembic upgrade head` before starting the web lifespan. Existing revision
   `0004` creates conversation access grants; Phase 6 requires no new migration.
4. Start one app worker. Lifespan recovery invalidates old grants and reconciles
   assignments before agent launches become eligible. Verify `/healthz`, then
   exercise authenticated conversation startup separately: health alone does not
   prove Gmail discovery or sandbox readiness.

`deploy/local.sh` already performs local builds, ownership setup, socket-group
resolution, database health, migration, and app startup in that order. It rebuilds
images, so do not use it merely to run pinned-image validation. Existing production
scripts also apply migrations before app startup; reviewing those scripts does
not authorize running a production deployment.

## Files, permissions, and network boundaries

Each generated conversation input directory is app-owned and 0755. Downloads
stage privately and atomically publish fresh per-call directories and 0644 files;
earlier explicit downloads remain available. The sandbox mounts only its own
assignment at `/input:ro`. `/workspace` is agent-owned writable scratch in the
container layer. Container removal discards copied inputs, output, processes,
and CLI state. Downloads are explicit tool outputs, not a mailbox cache.

Allocation uploads the exact packaged `agent_kit/CLAUDE.md` after readiness and
before returning, within the allocation deadline. The archive contains only
`CLAUDE.md`, installed at `/workspace/CLAUDE.md` with root ownership and mode 0644.
Read/upload failures log a static error and follow allocation cleanup. Fallback
reuses the existing copy; reset creates a new container with the current packaged
version. No installer, mount, entrypoint, or sandbox-image change is required.

Claude runs as `agent` in `/workspace` with non-TTY stream-json stdin/stdout,
`setsid --wait`, restricted mode, and `--no-session-persistence`. Registration uses
`--append-system-prompt-file /workspace/CLAUDE.md` in both Gmail and chat-only
launches. A synthetic marker check on Claude Code 2.1.292 found automatic project
context loading absent with the production restricted flags; the explicit file
flag loaded the marker in both launch modes without tool use. Registration uses
strict MCP configuration, `/input` as an additional directory, built-ins
`Read,Glob,Grep,Bash`, and `dontAsk`. Exact approvals cover the five Gmail tools,
`Read(//input/**)`, `Bash(ls *)`, and `Bash(rg *)`. Python is unapproved. The legacy
`sandbox.ask()` helper remains tool-disabled.

Permission rules are CLI policy, not an OS isolation boundary: broad `rg`
arguments can invoke preprocessors. `/input:ro` enforces input immutability for
shell subprocesses as well as file tools. Containers preserve non-root execution,
init, dropped capabilities, `no-new-privileges`, PID limit 2048, 2 GiB memory, one
CPU, bounded logs, no restart, no published ports, and no Docker socket mount.
Four sessions allow memory overcommit on the existing 4 GiB production host;
local performance measurements do not establish production capacity.

Sandboxes join only the configured bridge; backend services remain on the
Compose backend network. The app bridge exposes its other listener routes, and
outbound access remains unrestricted. Plain HTTP on the bridge is unencrypted.
Neither Docker DNS nor unpublished ports establish MCP-only isolation. Phase 6
records actual app-route, database, other-sandbox, host, and metadata reachability
without changing this accepted architecture. A blocked probe is evidence about
that tested setup and time, not a universal isolation guarantee. App-side Docker
socket access remains root-equivalent host control.

## Browser and transport

`POST /api/message` requires the authenticated cookie and CSRF token, returns
202 with conversation/turn IDs, and rejects overlapping prompts with 409.
`GET /api/conversation` includes transcript, sequence, turn state, failure, and
`gmail_status` (`pending`, `ready`, `unavailable`). SSE at
`GET /api/conversation/stream?conversation_id=...&after=...` replays sequenced deltas,
turn events, and availability. Stale cursors trigger snapshot reload. The browser
renders Markdown with raw HTML disabled. The separate unavailable notice survives
reload and turn completion. `POST /api/conversation/reset` retires the assignment.

Browser disconnect does not stop an accepted turn. Turns have a 120-second budget,
transcripts a cumulative 2 MiB limit, and replay retains 2048 events. A failed CLI
conversation requires reset. App restart loses conversation transcripts/context;
there is no automatic model retry or resume.

## Safe diagnostics and recovery

Use untargeted `uv run assistant-agent sandbox` for daemon/image/network readiness;
it creates no sandbox. Target an explicit assigned container for CLI version
inspection. Inspect allowlisted state only; avoid dumping Docker exec environment,
resolved Compose secrets, MCP headers/control responses, or message/tool bodies.
Routine logs should identify safe failure categories and versions, never grants,
Google/model credentials, or email content.

| Symptom | Check and recovery |
| --- | --- |
| Persistent Gmail-unavailable notice | Review safe startup category, internal `app:8000` route/network availability, root-owned config permissions, and installed CLI version. Reset to retry after fixing the cause. Continuing chat does not retry Gmail. |
| OAuth revoked/refresh rejected after successful discovery | Discovery verifies endpoint/tools, not Google API access. Use the connected-account reconnect flow, then reset. Never print stored credentials or refresh responses. |
| Docker unavailable or allocation blocked | Check daemon connection, configured image/network, socket group, and path mapping with untargeted status. Restore Docker and let reconciliation retry; do not bypass assignment tracking with a shared container. |
| Cleanup incomplete/capacity exhausted | Check owned container presence and safe cleanup category. A failed removal retains its slot and inputs until removal succeeds. Allow sweeper recovery; do not manually delete mounted inputs or unrelated containers. |
| Grant invalidation/revocation pending | Check database health and that migrations ran before lifespan. Launches remain gated during failed startup invalidation. Restore database access and allow retry. |
| Conversation lost after restart | Expected in-memory behavior. Start a new conversation; old grants/assignments are retired during recovery. |

## Validation commands and evidence

Run default regression/lint checks and the opt-in checks separately. Use the
existing built image; integration checks resolve and pin its image ID without
pulling `latest`. Docker checks need a reachable daemon. Installed-Claude checks
require an available model credential and make billable calls.

```bash
uv run pytest -q
uv run ruff check .
SANDBOX_DOCKER_INTEGRATION=1 uv run pytest -q tests/test_sandbox_async.py
CLAUDE_GMAIL_INTEGRATION=1 uv run pytest -q -s tests/test_claude_gmail_integration.py
GMAIL_MCP_INTEGRATION=1 uv run pytest -q -s tests/test_gmail_integration.py
```

The Phase 6 synthetic suite exercises the complete local app boundary with
synthetic mail. Its baseline and reachability checks run in the same suite;
`tests/gmail_phase6_checks.py` provides their helpers. To select those checks:

```bash
GMAIL_MCP_INTEGRATION=1 uv run pytest -q -s tests/test_gmail_integration.py \
  -k 'baseline or reachability'
```

The user selected the existing connected account and the user-selected messages for
live validation, without a known expected marker or attachment hash. Use safe
smoke mode in the app's configured environment:

```bash
uv run python -m assistant_agent.gmail_validation --smoke \
  --user-id INTERNAL_USER_ID --query SELECTED_QUERY \
  --report /private/tmp/gmail-live-smoke.json
```

The report identifies `validation_mode: live_smoke`. Smoke checks bounded search,
retrieval/thread identity consistency, attachment byte-count consistency when an
attachment exists, and five sequential live searches. A mailbox without an
attachment is reported as attachment coverage absent. This is not a controlled
fixture test and does not verify content against a known marker or independently
known attachment hash. The synthetic suite supplies exact fixture integrity
checks separately. The helper report omits IDs, headers, content, query, and
credentials; file reports are atomically written with private modes.

A future controlled fixture can use the same helper without `--smoke`, supplying
`--expected-marker` and `--attachment-sha256`. Those fixture arguments are excluded
in smoke mode. Keep private message selectors and content out of evidence notes.
A missing or failed live check leaves Phase 6 incomplete.

The connected local app was an older shared-sandbox deployment. Any refresh for
integrated live browser verification is limited to that local app, retaining the
existing credentials database and applied `0004`. This does not authorize
production deployment or a new migration.

| Phase 6 evidence | Result |
| --- | --- |
| Validation date and target Docker setup | 2026-10-07; local full-app integration harness. |
| Pinned sandbox image ID | `sha256:893c76059e11bb7f7898110caf0f829113a429fa9a5f063b1e914d145ac84a1c`. |
| Installed Claude / Codex versions | Claude Code 2.1.292 / Codex CLI 0.160.1 in the pinned image. |
| Full-app synthetic workflow, two-user isolation, lifecycle, degraded startup | All 5 integration tests passed in 105.90 seconds. |
| Existing-account live smoke | Passed at 2026-10-07T14:51:26Z; bounded message/thread consistency and five searches. Attachments uncovered; no known marker/hash. Live browser workflow remains pending: no browser provider was available and native Chrome initialization failed. |
| Concurrent 1/2/4-user connect/startup/search baseline | Passed; medians and sample counts below. Zero operation or sampling errors. |
| App/database/other-sandbox/host/metadata reachability | Recorded below for the local harness only. |
| Regression and Ruff | 399 passed, 11 skipped in 4.38 seconds; Ruff passed. Seven existing SQLite deprecation warnings. Helper tests: 47 passed. Docker transport regressions: 9 passed in 1.45 seconds. |

Local synthetic baseline (three rounds per concurrency level). The startup timer
includes synthetic OAuth connection and the initial message POST returning 202;
it does not measure container/Claude startup alone:

| Concurrent users | Startup samples | Median connect + initial POST | Search samples | Median search |
| --- | --- | --- | --- | --- |
| 1 | 3 | 0.839 s | 15 | 0.019 s |
| 2 | 6 | 0.878 s | 30 | 0.033 s |
| 4 | 12 | 0.937 s | 60 | 0.025 s |

There were zero operation or resource-sampling errors. These small synthetic
samples are a local baseline, not tail-latency guarantees or live Google/production
capacity measurements. No limits were changed from these results.

Bounded reachability checks from assigned containers observed the app health route
returning 200 and unauthenticated browser/MCP requests returning 401. Sibling and
host test listeners were reachable. Backend database DNS did not resolve, while
host port 5432 was reachable. The metadata-service TCP probe timed out; no metadata
content or credentials were requested. These observations apply only to the local
harness network. They confirm that direct app access and host/sibling reachability
remain accepted exposures; unresolved backend DNS does not imply database isolation
when the host database port is reachable.

Live service smoke passed at 2026-10-07T14:51:26Z with bounded message/thread
consistency and five sequential searches taking 0.322, 0.334, 0.312, 0.400, and
0.360 seconds. The report records `validation_mode: live_smoke` and
`attachments_covered: false`. No controlled marker or independent attachment hash
was supplied. **Phase 6 remains incomplete**: live browser workflow and controlled
live attachment validation remain pending. Synthetic attachment integrity is
covered, but cannot substitute for those live checks.

The current local app image built successfully, but the existing connected app
was not restarted: no browser provider was available, and native Chrome failed
with “Sky Computer Use native pipe startup failed.” No database migration or
production deployment was performed. The remaining browser step is to refresh
only the local app using the built image and retained credentials database, then
verify authenticated live conversation discovery, the selected mail workflow,
local downloads, reset, and the persistent unavailable notice in a working browser.
Controlled live attachment validation still requires a known attachment fixture.

Additional Docker transport regressions passed: 9 tests in 1.45 seconds, using an
existing configured Docker network with isolated test deployment labels/input
roots. An initial setup attempt failed because the default sandbox network was
absent; the corrected run used the available network. This verifies transport in
that setup, not production deployment or live browser behavior.

Phase 5's earlier installed-CLI evidence is recorded in
[gmail-mcp.md](gmail-mcp.md#phase-5--claude-registration-and-permissions). It does
not substitute for Phase 6 full-app synthetic or live-account evidence. Production
rollout, caching, warm pools, HTTP batching, network redesign, and broader command
permissions remain outside this local validation work.
