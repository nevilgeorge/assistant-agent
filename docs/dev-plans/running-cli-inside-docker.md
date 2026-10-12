# Running agent CLIs inside Docker

Last updated: 2026-10-12. The Gmail Phase 6 evidence below records its own
remaining validation. Daily-log infrastructure and local wiring are described
here; host acceptance and production rollout remain separate, explicit steps.
See the [daily-logs development plan](daily-logs.md) for implementation and
validation status.

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
| `SANDBOX_HOST_USER_DATA_ROOT` | Persistent user data as seen by the Docker daemon: local `<repo>/data/user-data`; production `/srv/assistant-agent/s3/user-data`. Keep separate from input roots. |
| `SANDBOX_APP_USER_DATA_ROOT` | Same data as seen by the app: local Compose `/srv/assistant-agent/user-data`; production `/srv/assistant-agent/s3/user-data`. Match the host path for direct host execution. |
| `CHAT_MAX_SESSIONS` | Default 4; counts pending allocation, live containers, and failed removal. |
| `CHAT_IDLE_SECONDS` | Default 1800 seconds; grant expiry remains a separate fixed maximum. |

Gmail concurrency, quota, request-size, and file-size limits are constructor
settings in `GmailLimits` and session/MCP services, not additional environment
variables. Discovery and startup budgets are code constants. Do not invent
configuration knobs when investigating a timeout.

For a new or upgraded local app, the existing startup order is:

1. Make the selected app/sandbox images available and create the configured Docker
   network, input storage, and persistent user-data storage. Both separate roots
   belong to app UID/GID 10001 with mode 0700; the app needs access to the Docker
   socket's group. Initialization changes only root ownership/mode and preserves
   files on reruns.
2. Start PostgreSQL and wait for health.
3. Run `alembic upgrade head` before starting the web lifespan. Existing revision
   `0004` creates conversation access grants; daily logs add the user-timezone
   migration. Apply all current migrations.
4. Start one app worker. Lifespan recovery invalidates old grants and reconciles
   assignments before agent launches become eligible. Verify `/healthz`, then
   exercise authenticated conversation startup separately: health alone does not
   prove Gmail discovery or sandbox readiness.

`deploy/local.sh` already performs local builds, ownership setup, socket-group
resolution, database health, migration, and app startup in that order. It rebuilds
images, so do not use it merely to run pinned-image validation. Existing production
scripts also apply migrations before app startup; reviewing those scripts does
not authorize running a production deployment.

For direct local Compose commands, export both paths before resolving Compose:

```bash
export SANDBOX_HOST_INPUT_ROOT="$(pwd -P)/data/session-inputs"
export SANDBOX_HOST_USER_DATA_ROOT="$(pwd -P)/data/user-data"
```

For direct host execution, also export `SANDBOX_APP_INPUT_ROOT` equal to
`SANDBOX_HOST_INPUT_ROOT`, and `SANDBOX_APP_USER_DATA_ROOT` equal to
`SANDBOX_HOST_USER_DATA_ROOT`. Create both roots with mode 0700 and app runtime
ownership; do not point either setting at the other storage tree.

## Files, permissions, and network boundaries

Each generated conversation input directory is app-owned and 0755. Downloads
stage privately and atomically publish fresh per-call directories and 0644 files;
earlier explicit downloads remain available. The sandbox mounts only its own
assignment at `/input:ro`. `/workspace` is agent-owned writable scratch in the
container layer. Container removal discards copied inputs, output, processes,
and CLI state. Downloads are explicit tool outputs, not a mailbox cache.

Persistent daily logs live at `<user-data-root>/<user_id>/daily-logs/` and only
that user’s directory is bound to `/daily-logs:rw` in a sandbox. Cleanup and reset
delete disposable inputs and in-memory conversation history, never daily logs.
The app creates `YYYY-MM-DD.md` files with a date/timezone header; Claude reads
first and updates existing files, with guidance not to create, rename, or delete
them. The stored timezone takes precedence, otherwise the app fetches the primary
Google Calendar timezone, then falls back to UTC without saving the fallback.
Every turn supplies `Current user time: <ISO 8601 timestamp with offset>` to Claude
without changing the visible user prompt. That same captured time determines
today’s app-created file, including rollover across midnight.

The local user-data root is 0700; the app creates user directories that allow
the sandbox to traverse only its mounted subtree, and daily-log directories/files
with 0777/0666 for local UID compatibility. Production s3fs presents UID 1000,
GID 10001, and mask 0007. Verify both sandbox UID 1000 and app UID/GID 10001 access
on the production mount. These permissions do not coordinate multiple writers.
Keep the single-instance deployment; same-user writer coordination is deferred.

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
`sandbox.ask()` helper remains tool-disabled. A sandbox with daily logs also
receives `/daily-logs` as an additional directory, `Edit,Write` built-ins, and
`Read(//daily-logs/**)` / `Edit(//daily-logs/**)` approvals (Edit governs Write).
These grants do not approve editing `/workspace` or `/input`.

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
socket access remains root-equivalent host control. Once the production instance
role has application S3 permissions, a sandbox that reaches instance metadata
could access other users’ `user-data` prefixes. This is an accepted risk; metadata
isolation remains a separate follow-up.

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

## Production daily-log storage

Production uses pinned s3fs v1.95 with FUSE 2 and EC2 instance-role credentials.
The deployment image packages the units/helpers; deployment extracts them without
changing instance user data. Configuration is root-only at
`/etc/assistant-agent/storage.conf`; Compose and secrets remain under
`/opt/assistant-agent`. Do not print the resolved Compose configuration or `.env`.

Startup, including after reboot, is ordered through systemd:

1. `assistant-daily-logs.service` runs guarded cleanup and preparation before foreground
   s3fs. It verifies the EBS UUID and ext4 filesystem, preserves the root-owned 0700
   cache at `/srv/assistant-agent/s3fs-cache`, and protects the empty uncovered
   `/srv/assistant-agent/s3/user-data` mountpoint with `chattr +i`. Missing/wrong EBS,
   symlinks, unexpected mounts, or nonempty uncovered directories block startup.
   It mounts the bucket’s `user-data` prefix, verifies its FUSE type/source, and
   performs a write/flush/close/read/delete probe as UID/GID 10001. The probe deadline
   is 30 seconds; storage activation allows 180 seconds including preparation.
2. `assistant-app.service` requires Docker and ready storage, verifies database health,
   and repeats the probe through the app image and production bind. Deployment runs
   migrations before app startup. Compose force-recreates only the app and remains
   attached so systemd observes its exit code and restarts app failures. Production
   app Compose has `restart: "no"` to avoid competing supervision. An `ExecStartPost`
   probe verifies the actual recreated app container before activation succeeds.

The app directly binds `/srv/assistant-agent/s3/user-data` at the same path with
private propagation and `create_host_path: false`. There is no shared parent mount.
Every replacement mount requires removing its old consumers and recreating the app.
The storage service has `Restart=no`; `Requires`/`After` and `BindsTo` stop the app
when storage disappears. A live process alone does not guarantee successful storage
I/O; investigate filesystem failures and run the probes. Recovery remains explicit.

Deployment entrypoints and ownership are described in the
[deployment guide](../../deploy/README.md). The self-contained
`deploy/production/bootstrap.sh` pulls images and extracts the deployment bundle;
`production/apply.sh` handles host preparation, storage changes, database migration,
and application startup. Runtime helpers and service units live under
`deploy/production/runtime/` and `deploy/production/systemd/`; their installed paths
and the flat image bundle at `/app/deploy` remain unchanged.

First setup or deliberate binary repair uses `./deploy/deploy.sh --setup-s3fs`.
The installer builds checksum-verified v1.95 source with FUSE 2 and limited build
parallelism, preserving Amazon Linux’s minimal curl packages. It stages and validates
replacement binaries before downtime, and installs them only after retiring consumers
and stopping the old mount. Ordinary deployment runs only `install-s3fs.sh check`:
version, executable architecture, shared libraries, and FUSE 2 linkage must pass before
the app is stopped. This check makes no package changes or network requests.

Migration from the previous three-unit configuration retains the old helpers and
configuration until old consumers and FUSE mounts are gone. The old parent self-bind
is removed only after verifying its expected EBS UUID, ext4 type, and `/s3` filesystem
root. Unexpected mounts abort migration. The obsolete preparation unit is disabled
and removed; rerunning after interruption tolerates already-removed legacy resources.

A repeated deployment preserves a healthy unchanged mount. Changed storage
configuration requires stopping the app and retiring sandboxes before replacing
the mount. An inactive unchanged mount makes deployment fail visibly; run the
operator recovery helper instead of treating deployment as recovery. App stop removes remaining containers using both the assistant owner
and deployment labels; unrelated containers and stored daily logs remain intact.
Sandbox private binds do not recover after a host remount, so never repair a
mount while retaining its old conversations.

### Manual recovery and reboot checks

Run the recovery helper on the production host:

```bash
sudo /usr/local/libexec/assistant-agent/recover-daily-logs.sh
```

It stops and removes the app, retires remaining sandboxes even if the app unit is
inactive, verifies consumer retirement (including transient app containers), stops and
unmounts storage, starts and probes the mount, then recreates and probes the app. A failed step aborts
with the app stopped. Stale cleanup is restricted to the configured mountpoint
and expected FUSE filesystem; cleanup failure must be investigated before retry.
Do not bypass a failure with a broad unmount, cache deletion, or an unprotected
plain directory. Cache files may contain data from failed writes.

For an explicit maintenance stop use `sudo systemctl stop assistant-app.service`.
After repairing storage, use the recovery helper. For a planned reboot, first
confirm both units are enabled, stop the app cleanly, then reboot during an
approved maintenance window. On boot, verify the mount and both identity probes
before testing a new authenticated conversation. Confirm the service ordering in
the boot journal, and independently read back a known daily entry from S3. Test
failure propagation and boot ordering in a disposable systemd Linux environment
before production rollout; this document does not claim those checks have run.

### Diagnostics and persistence checks

```bash
sudo systemctl is-enabled assistant-daily-logs assistant-app
sudo systemctl status assistant-daily-logs assistant-app
sudo journalctl -b -u assistant-daily-logs -u assistant-app
findmnt --mountpoint /srv/assistant-agent
findmnt --mountpoint /srv/assistant-agent/s3/user-data -o TARGET,SOURCE,FSTYPE,OPTIONS
sudo /usr/local/libexec/assistant-agent/daily-logs-host.sh ready
sudo /usr/local/libexec/assistant-agent/app-host.sh check
df -h /srv/assistant-agent
sudo du -sh /srv/assistant-agent/s3fs-cache
```

The final mount must be the expected `fuse.s3fs` filesystem. The cache lives on
EBS; `ensure_diskfree=1024` reserves headroom and **is not a cache quota** or a
guarantee against disk exhaustion. Monitor available space and investigate failed
uploads before clearing anything. Probe failures log categories without file
contents; avoid sharing private daily entries in incident output.

With operator credentials scoped for the intended bucket and user, verify S3
independently of the mount (replace placeholders locally):

```bash
aws s3 ls s3://BUCKET/user-data/USER_ID/daily-logs/ --region REGION
aws s3 cp s3://BUCKET/user-data/USER_ID/daily-logs/YYYY-MM-DD.md /tmp/daily-log-readback.md --region REGION
```

Inspect that private local file, then remove it when finished. Verify app
UID/GID 10001 through the app probe and sandbox UID 1000 through a real assigned
sandbox’s daily-log read/edit. An HTTP health check alone cannot establish
storage permissions or persistence. In a disposable host check, stop app/storage
and verify a write to the uncovered immutable mountpoint fails, including as
root; never disable its immutable protection to make startup succeed.

### Rollback and S3 version recovery

Stop `assistant-app.service` and run `app-host.sh stop` to remove all remaining
consumers before rolling back an app image or restoring a log. Lifecycle rollback
requires restoring the previous matching units, helpers, and Compose configuration
together after stopping storage; never mix assets from the two lifecycles.
Retain the EBS cache, bucket, versioning, and mountpoint protection. Review migration
compatibility before deploying a prior image, then run readiness and start through
systemd. Do not restore a Compose app restart policy that could start before
storage is ready. If mount configuration also changes, use the same ordered
stop/retire/unmount/start/probe sequence; leave the app stopped on any failure.

Bucket versioning preserves overwritten/deleted objects. The instance role has no
version-deletion permission and no version-read/list grant; use separately
authorized operator credentials to select and recover an earlier version:

```bash
aws s3api list-object-versions --bucket BUCKET --prefix user-data/USER_ID/daily-logs/YYYY-MM-DD.md --region REGION
aws s3api get-object --bucket BUCKET --key user-data/USER_ID/daily-logs/YYYY-MM-DD.md --version-id VERSION_ID /tmp/recovered-daily-log.md --region REGION
```

Review the recovered file privately. With the app and sandboxes stopped and the
mount healthy, copy it back through the mounted path, preserving the intended
ownership/permissions, then perform independent S3 readback. Restoring through the
mount keeps its cache consistent and creates a new S3 version. Do not permanently
delete historical versions as part of recovery. If the mount is unhealthy, repair
and probe it before restoring or starting the app.

Operational follow-ups: consider S3 access logging, CloudTrail data events for the
user-data prefix, and CloudWatch metrics/alarms for storage errors and disk space.
These incur additional request, logging, retention, or metric costs; no new
monitoring infrastructure is included here. Metadata isolation and cross-instance
or same-user writer coordination remain deferred as recorded in the
[daily-logs plan](daily-logs.md).

### Local daily-log acceptance

Run `deploy/local.sh`, sign in, and ask the assistant to record a harmless test
entry. Verify `data/user-data/<user_id>/daily-logs/<today>.md`, inspect the app
mounts and the assigned sandbox’s `/input:ro` and `/daily-logs:rw` mounts, then
reset and ask the assistant to reread the entry. Stop/restart the app and confirm
cleanup removed disposable input directories but preserved the daily file. Keep
this acceptance separate from production S3, reboot, and failure tests.

Isolated local storage acceptance passed on 2026-10-12 using existing app image
`d6d8efbf19d7` with current source mounted read-only and sandbox image
`3880e2a0cd82` on OrbStack. In separate temporary directories, the app UID 10001
created a daily file through `DailyLogService`; sandbox UID 1000 read its input,
was denied an input write, and appended/flushed/reread the daily entry. Removing
the test container and disposable inputs, then repeating root initialization,
preserved the entry for a replacement sandbox. No labeled test containers
remained. OrbStack reported root ownership as 0:0 after successful chown to
10001:10001; actual identity access and mode 0700 were checked, but this does not
verify native Linux ownership semantics. No running app or real account was used;
authenticated reset, actual Claude editing, and S3/host acceptance remain pending.

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
