# Daily agent log files on an S3-backed FUSE mount

Status: Phases 1–6 implemented; Phases 7–8 code and documentation implemented; host/production acceptance pending. Last updated: 2026-10-12

## Context

The sandboxed assistant agent has no persistent storage: `/workspace` dies with the container and
`/input` is read-only. This feature gives each agent a scratch pad of their own -- only the agent can read/write to these files, and it allows them to keep a record of daily activities, reminders, and anything else pertinent to be a helpful personal assistant. 
Each user has a folder of daily markdown files (`YYYY-MM-DD.md`) that the agent creates and maintains.

The files live in the existing application-data bucket under `user-data/<user_id>/daily-logs/`. The EC2
host mounts that prefix with s3fs, and every sandbox gets its user's logs folder bind-mounted at
`/daily-logs` read-write, so agent writes land in S3.

Naming: the feature is always "daily logs" (`daily-logs` in paths, `daily_logs` / `daily_log` in
code), never bare "logs", so it cannot be confused with application logs.

Decisions already taken:

- **FUSE client:** s3fs (supports rename, append and in-place edits).
- **Scope:** the user's whole logs folder, all days, read-write.
- **Day boundary:** the user's primary Google Calendar timezone, UTC fallback.
- **Instance-metadata lockdown:** separate follow-up; recorded here as an accepted risk.
- **Local dev:** a plain directory under `data/user-data` instead of S3.
- **Recovery:** operator-invoked only; stop the app and its sandboxes, repair and probe storage,
  then restart the app. No automatic remount, recovery timer, or FUSE restart loop.
- **Startup:** systemd owns production app supervision and orders every startup, including reboot,
  after ready storage and Docker. Compose does not restart the app.
- **Coordination:** keep the single-instance deployment; defer per-user writer coordination.
- **Rollout:** infrastructure/code changes are distinct from explicit Terraform apply, production
  deployment, and host acceptance; implementation does not imply those checks passed.

## Design

### Agent prompt & date resolution

- Add a "Daily logs" section to the tracked `src/assistant_agent/agent_kit/CLAUDE.md`.
  It describes where the daily files live (`/daily-logs/YYYY-MM-DD.md`), how to read and
  update them, and when to consult previous entries and record useful information. The
  sandbox copies this file to `/workspace/CLAUDE.md`; Claude CLI loads it through the existing
  `--append-system-prompt-file` flag.
- The app resolves the user's stored timezone (with the UTC fallback described below) and
  sends the current user-local time with **every request**, including later turns in an
  existing conversation. The agent does not query the users table.
- Send one ISO 8601 timestamp with its UTC offset, for example
  `Current user time: 2026-10-12T14:35:00-04:00`. Do not also send the timezone name or today's
  log path: the agent derives the local date and `/daily-logs/2026-10-12.md` from that timestamp
  using the convention documented in `CLAUDE.md`.
- Include the timestamp as app-generated context in the same `type: "user"` JSON message
  written to Claude's stdin. It is ordinary message content, not a separate system message.
  Keep the original user prompt in the visible transcript and send the augmented content only
  to Claude. `CLAUDE.md` tells the agent to use the latest supplied timestamp for date resolution.
- Capture the timestamp once for each turn and use its local date both to ensure the daily
  file exists and to construct the context sent to Claude. This keeps the file and the
  agent's date consistent even if preparation crosses midnight.

### Host mount (production)

- s3fs mounts `<bucket>:/user-data` at `/srv/assistant-agent/s3/user-data`.
- The app container directly binds `/srv/assistant-agent/s3/user-data` at the same path
  with private propagation and `create_host_path: false`. Storage starts before containers
  are created; recovery removes consumers before remounting and force-recreates the app.
- The empty mountpoint is made immutable (`chattr +i`) before mounting. If s3fs is not mounted,
  the app, root and the Docker daemon all fail to write instead of silently writing to EBS.
- Sandboxes bind `<host root>/<user_id>/daily-logs` to `/daily-logs` with no propagation flag.
- Ownership via mount options: `uid=1000` (sandbox agent), `gid=10001` (app), `umask=0007`.

### s3fs-fuse mount details

- **Responsibility:** the agent uses ordinary file reads and writes under `/daily-logs` and
  does not need to know whether content has propagated to S3. s3fs automatically uploads modified
  content through its flush/fsync and file-release handlers; there is no application sync loop.
  Persistence monitoring and recovery belong to the application and host. Storage failures can
  still surface to the agent as ordinary filesystem errors.
- **Host process:** one s3fs process on the EC2 host serves the shared mount used by the app
  and sandbox bind mounts. Run it with `-f` under systemd so it does not daemonize: systemd tracks
  the foreground process while it operates as a background service for the application.
- **Installation:** Amazon Linux 2023 already includes systemd; the instance bootstrap already
  uses `systemctl` for Docker and SSM. Only s3fs needs installation. Pin s3fs to v1.95 (FUSE 2).
  Explicit `--setup-s3fs` builds the verified release tarball once. Ordinary deployment
  only checks version, architecture, runtime libraries, and FUSE 2 linkage before downtime.
- **Single mount owner:** systemd owns startup and shutdown; recovery is manual. Do not also install an fstab entry
  for this mount or independently mount it from deployment.

Track `deploy/production/systemd/assistant-daily-logs.service` as a plain-text systemd unit, not XML. Bundle it in
the app image alongside Compose and Caddy configuration. `deploy/production/bootstrap.sh` extracts the packaged host assets;
`deploy/production/apply.sh` writes validated bucket, region,
volume identity and deployment configuration, and installs the unit as
`/etc/systemd/system/assistant-daily-logs.service`. The launch configuration is:

```ini
[Unit]
Description=Assistant daily logs S3 filesystem
Wants=network-online.target
After=network-online.target
RequiresMountsFor=/srv/assistant-agent

[Service]
Type=exec
User=root
ExecStart=/usr/local/bin/s3fs <bucket>:/user-data /srv/assistant-agent/s3/user-data \
    -f \
    -o fsname=<bucket>:/user-data \
    -o iam_role=auto \
    -o endpoint=<region> \
    -o url=https://s3.<region>.amazonaws.com \
    -o allow_other \
    -o uid=1000,gid=10001 \
    -o umask=0007,mp_umask=0007 \
    -o compat_dir \
    -o use_cache=/srv/assistant-agent/s3fs-cache \
    -o check_cache_dir_exist \
    -o ensure_diskfree=1024 \
    -o connect_timeout=10 \
    -o readwrite_timeout=30 \
    -o retries=3 \
    -o dbglevel=warn
Restart=no
TimeoutStartSec=180
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

This is the mount command specification; the tracked unit additionally orders preparation,
executes bounded readiness checks, and cleans up only the expected stale FUSE mount. `Type=exec` confirms that the
executable launched, not that the mount is ready.

| Parameters | Purpose |
| --- | --- |
| `iam_role=auto` | Use the EC2 instance role without storing static credentials. |
| `fsname`, `subtype=s3fs` | Set the expected bucket/prefix source and `fuse.s3fs` type. FUSE 2 requires the subtype explicitly when `fsname` is supplied. |
| `endpoint`, HTTPS `url` | Use the bucket's region and encrypted transport. |
| `allow_other`, UID/GID, masks | Give the sandbox UID 1000 and app GID 10001 read/write access, excluding others. Verify effective permissions with both identities. |
| `compat_dir` | Support the existing prefix layout uploaded outside s3fs. Verify first mount with the scoped IAM policy. |
| `use_cache`, `check_cache_dir_exist` | Use a pre-created root-only `0700` cache directory on EBS. |
| `ensure_diskfree=1024` | Reserve 1024 MB of disk headroom; this is not a hard cache quota or a guarantee against disk exhaustion. |
| `connect_timeout=10`, `readwrite_timeout=30`, `retries=3` | Bound individual network attempts; the read/write timeout measures inactivity and retries can extend total operation time. |
| `dbglevel=warn` | Expose operational failures without verbose request logging. |

Keep normal upload defaults initially; do not tune multipart thresholds or enable experimental
streaming uploads for daily Markdown files. Reference: [s3fs v1.95 manual](https://raw.githubusercontent.com/s3fs-fuse/s3fs-fuse/v1.95/doc/man/s3fs.1.in)
and [write/flush implementation](https://raw.githubusercontent.com/s3fs-fuse/s3fs-fuse/v1.95/src/s3fs.cpp).

Deployment and operations:

- Prepare the cache and immutable empty mountpoint after the EBS data volume is mounted.
  Preparation runs in ordered storage-service `ExecStartPre` commands.
- After installing the unit, run `systemctl daemon-reload` and
  `systemctl enable --now assistant-daily-logs.service`. Enable starts it on future boots;
  network and data-volume dependencies order startup. Avoid restarting a healthy mount on
  every deployment; apply configuration changes with conversation retirement when needed.
- Before admitting conversations, verify the mount and perform a bounded application-owned
  write/read/delete probe. A successful `stat` alone does not prove write access.
- A failed mount stops the dependent app through systemd `BindsTo`. Recovery is manual:
  stop app, retire its sandbox containers using both ownership and deployment labels, stop and
  clean up the configured stale FUSE mount, start and probe storage, then start the app. Abort
  on any failure, leaving the app stopped. Never use the underlying EBS directory as fallback.
- After a remount, retire affected sandbox conversations: their private bind mounts are not
  repaired by restarting s3fs. New conversations use the recovered host mount.
- Monitor service failures, filesystem errors, cache/disk space, and a periodic storage probe.
  A living process does not guarantee healthy storage. Inspect with
  `systemctl status assistant-daily-logs.service` and
  `journalctl -u assistant-daily-logs.service -f`.
- Before production acceptance, prove actual Claude edits, S3 readback, reset/reread,
  effective permissions, scoped IAM, boot startup, and remount recovery on the production host.
  Also exercise failed uploads and process termination during writes; recovery is an
  application/host concern, not an agent instruction.

Filesystem workers are bounded and tracked separately from mount options because timing out an
`asyncio.to_thread` await does not terminate a stalled FUSE call. Phase 3 retains worker admission
until I/O finishes and provides a bounded, resumable shutdown drain. For now, assume that reset or
retirement will not overlap a replacement daily-log writer; no per-user conversation ownership
guard is implemented. Cross-instance coordination remains out of scope.

### App flow

1. `ConversationManager._spawn` resolves the user's timezone, captures the turn timestamp,
   and calls `DailyLogService.prepare(user_id, timezone, timestamp)`,
   which creates the logs folder and today's file using that captured local date.
2. It passes the resulting host path to `Sandbox.allocate(..., host_daily_logs_path=...)`. Preparation
   stays out of `Sandbox._create_and_provision_sandbox`, which holds the global lock.
3. `_container_config` adds `f"{handle.host_daily_logs_path}:/daily-logs"` when the handle has a logs path.
4. `ClaudeProcess.create` enables `Edit`/`Write` scoped to `/daily-logs` and adds log guidance.
5. **Midnight rollover:** in `_submit`, for an existing process, call `_begin_turn` first and then
   await the "ensure today's file" step inside the existing `try`/timeout before `process.send`
   (`conversation_manager.py:197-205`). Awaiting before `_begin_turn` would let two submits both
   pass the busy check at `:191`. It is a no-op unless the local date changed.

Failure policy: a failed prepare at spawn fails the spawn (503 through the existing handler at
`conversation_manager.py:309`). A failed rollover logs a warning and the turn continues.

### Agent permissions and guidance (`claude_process.py`)

- `--tools Read,Glob,Grep,Bash,Edit,Write` and `--add-dir /daily-logs` (required: `--restricted`
  confines file tools to the added directories).
- Approvals: `Read(//daily-logs/**)` and `Edit(//daily-logs/**)` (the Edit rule also governs Write).
- Guidance lives in the "Daily logs" section of `agent_kit/CLAUDE.md`, loaded through the existing
  `--append-system-prompt-file` flag, as described in "Agent prompt & date resolution".
- Guidance content: `/daily-logs` holds one `YYYY-MM-DD.md` per day; use the current user time
  supplied with the request to identify today's file; never create, rename or delete files there; read first, then
  add short entries while preserving existing content.

## Implementation sequence

Each phase keeps `uv run pytest -q` and `uv run ruff check .` green.

### Phase 1 — Settings

Status: Implemented

- `src/assistant_agent/config.py`: add `host_user_data_root` / `app_user_data_root` to
  `SandboxSettings` (default `DATA_DIR / "user-data"`), read from `SANDBOX_HOST_USER_DATA_ROOT` /
  `SANDBOX_APP_USER_DATA_ROOT`, with the existing absolute/non-root/no-`..` validation.
- Reject overlapping input and user-data roots: `_reconcile` deletes every 32-hex directory under
  the input root (`sandbox.py:313-318`), and user ids are 32 hex.
- `tests/test_sandbox_lifecycle.py`: extend `test_settings_validation`.

### Phase 2 — User timezone

Status: Implemented

- `src/assistant_agent/database.py`: nullable `User.timezone` (`String(64)`).
- New Alembic revision adding the column.
- New `src/assistant_agent/user_timezone.py`:
  - `fetch_primary_calendar_timezone(credentials) -> str | None`, using
    `calendars().get(calendarId="primary")["timeZone"]`, built like `gmail_service.py:468-478`
    with a short HTTP timeout.
  - `valid_timezone(name) -> str | None` via `zoneinfo.ZoneInfo`.
  - `UserTimezoneService.resolve(user_id) -> str`: stored value, else lazy fetch and store, else
    `"UTC"` without storing so the next spawn retries.
- `src/assistant_agent/web_store.py`: `save_timezone(user_id, timezone_name)`.
- `src/assistant_agent/web.py`: best-effort fetch in the OAuth callback after `save_credentials`.
- Check the app image has tz data; add the `tzdata` dependency to `pyproject.toml` if not.
- Tests: new `tests/test_user_timezone.py`; migration test modelled on
  `tests/test_sandbox_access_migration.py`; patch the fetch in the `web` fixture
  (`tests/test_web_auth.py`).

### Phase 3 — Implement DailyLogs module (`src/assistant_agent/daily_logs.py`)

Status: Implemented

- `DailyLog` dataclass: `host_daily_logs_path`, `app_daily_logs_path`, `local_date`, `timezone_name`.
- Provide an injectable clock for capturing an aware user-local timestamp. Derive the local
  date from that captured value; do not independently read the clock during file preparation.
- `async DailyLogService.prepare(user_id, timezone_name, current_user_time) -> DailyLog`,
  idempotent, using the supplied timestamp's local date:
  - validate `user_id` as 32 hex and reject symlinks, mirroring `Sandbox._session_path`
    (`sandbox.py:113-124`);
  - create the logs directory, then the file with `O_CREAT|O_EXCL|O_NOFOLLOW` and a
    `# YYYY-MM-DD (<tz>)` header;
  - on creation only, chmod directory `0o777` and file `0o666`, ignoring `PermissionError`. This
    is what lets uid 1000 write in local dev; on s3fs the mount options decide.
- **Warm-up:** after ensuring today's file, `prepare` reads the files for the previous 7 local
  dates that exist (by filename, no directory walk) and discards the content. On s3fs this fills
  the local cache before the sandbox starts; on a plain directory it is a harmless no-op. A
  failed warm-up read is logged and ignored. The window is a module constant, not a setting.
- Blocking filesystem calls run via `asyncio.to_thread` under a timeout, as in
  `session_files.py`, with bounded admission and tracked completion/shutdown handling.
  A timed-out await must not permit unbounded accumulation of stalled workers.
- Add an idempotent ensure-file operation for later turns, using the captured turn timestamp
  without repeating startup warm-up. The service does not implement an S3 synchronization loop.
- New `tests/test_daily_logs.py`: non-UTC date across midnight, idempotent create keeps content,
  bad user id, symlink rejection, modes, warm-up reads only the last 7 dated files and tolerates
  missing or unreadable ones; preparation uses the supplied date even if the clock crosses
  midnight while it runs; worker admission remains bounded under stalled I/O.

### Phase 4 — Sandbox bind

Status: Implemented

- `src/assistant_agent/sandbox.py`:
  - `SandboxHandle.host_daily_logs_path: Path | None = None`.
  - `allocate`, `_allocate_with_timeout`, `_create_and_provision_sandbox` take keyword-only
    `host_daily_logs_path`, verify
    it is under `settings.host_user_data_root`, and carry it through both handle constructions
    (`:201`, `:215`).
  - `_container_config` appends the `/daily-logs` bind when set.
  - `_destroy` and `_reconcile` stay unchanged and never touch logs.
- Because the field defaults to `None`, existing direct-`allocate` tests keep one bind.
- `tests/test_sandbox_lifecycle.py`: two-bind test, path-outside-root rejection, and a check that
  destroy/reconcile leave a logs tree alone.

### Phase 5 — Agent permissions and prompt

Status: Implemented

- `src/assistant_agent/chat/claude_process.py`: build `--tools`, `--add-dir` and approvals in
  `create` from `sandbox_handle.host_daily_logs_path`; retain the existing prompt-file loading.
- `src/assistant_agent/agent_kit/CLAUDE.md`: add the "Daily logs" section and date-resolution
  guidance described above: paths, reading/updating files, consulting previous entries, and
  recording useful information. Use the latest request timestamp to derive today's filename.
  The agent uses ordinary filesystem tools and is not responsible for checking S3 propagation.
- Run `uv run assistant-agent install-kit --force` to refresh installed copies and
  `uv run assistant-agent install-kit --check` to verify they match the tracked source.
- `tests/test_claude_process.py`: flags present with a logs handle, absent without.
  Extend context-provisioning/integration tests to verify the actual copied prompt and its
  loading by the installed Claude CLI; exercise daily-log reads/writes and denial outside
  the permitted paths.

### Phase 6 — Conversation wiring and per-request user time

Status: Implemented

- `src/assistant_agent/chat/conversation.py`: `daily_log: DailyLog | None`.
- `src/assistant_agent/chat/conversation_manager.py`: `daily_log_service` and
  `user_timezone_service` constructor parameters. Resolve the timezone at spawn; capture one
  current user-local timestamp for each request, including the first request.
- Prepare before `allocate` in `_spawn` using that timestamp. For an existing process, call
  `_begin_turn` before awaiting rollover, inside the turn timeout, and use the same timestamp
  for the ensure-file operation and the message sent to Claude.
- Augment the stdin user-message content with `Current user time: <ISO 8601 timestamp with
  UTC offset>` on every request. Do not additionally send a timezone name or daily-log path.
  Retain the original user prompt in the visible transcript; the augmented content is only
  sent to Claude. Test the extra content against protocol-size limits.
- Assume reset or retirement does not overlap a replacement daily-log writer for now;
  defer same-user conversation ownership handling.
- `src/assistant_agent/web.py` lifespan: construct both services, pass them in, and close/drain
  daily-log worker resources during shutdown using the lifecycle handling from Phase 3.
- Tests: `FakeSandbox.allocate` gains `host_daily_logs_path=None` (`tests/test_chat.py:26`); fake
  daily-log service and clock; tests for spawn failure, rollover call/failure, no double turn,
  first and later request timestamps, non-UTC offsets, midnight consistency, unchanged visible
  transcripts, and cancellation cleanup.
  `tests/gmail_phase6_checks.py:270` expects two binds (it runs the real lifespan).

### Phase 7 — Terraform, systemd, and deployment

Status: Code implemented; native AL2023/systemd and production acceptance pending.

- `terraform/main/application-data.tf`: `aws_iam_role_policy` on the instance role with
  `s3:ListBucket` limited to the `user-data` prefix, and `s3:GetObject`, `s3:PutObject`,
  `s3:DeleteObject`, `s3:AbortMultipartUpload` on `<bucket>/user-data/*`. No
  `DeleteObjectVersion`, so bucket versioning keeps a recoverable history of every overwrite.
- `deploy/deploy.sh`: read the `application_data_bucket_name` output and pass it and the
  deployment region through `production/bootstrap.sh` to `production/apply.sh` for
  rendering the service configuration.
- `deploy/production/systemd/assistant-daily-logs.service` and `Dockerfile`: track and bundle the service unit
  alongside the existing deployment configuration. Use the command/options from
  "s3fs-fuse mount details": `Type=exec`, root execution, s3fs `-f`, instance-role credentials,
  HTTPS region endpoint, UID/GID/masks, cache settings, timeouts/retries, warning logging,
  `Restart=no`, and a 180-second startup deadline. Order startup after networking and verified
  EBS preparation. The app unit uses `Requires`/`After`/`BindsTo` and `Restart=on-failure`;
  production Compose uses `restart: "no"`.
- `deploy/production/apply.sh`: check s3fs before downtime; explicit `--setup-s3fs` stages a verified
  source build before stopping consumers and installs it after stopping storage. Storage
  preparation creates the `0700` cache and immutable empty mountpoint. Install
  the two systemd units; reload systemd and enable/start
  the service. Systemd is already present on AL2023; do not install it or add a competing fstab
  entry. Avoid restarting a healthy unchanged service on every deployment; retire affected
  conversations when applying mount configuration changes.
- Add the host readiness and stale-mount cleanup helpers needed by the service. A successful
  process launch or `stat` is insufficient: require a bounded mount and application-owned
  write/read/delete probe, and exit before app startup if storage readiness fails.
- `deploy/production/compose.prod.yaml`: both env vars set to `/srv/assistant-agent/s3/user-data`; long-syntax
  bind of `/srv/assistant-agent/s3/user-data` with private propagation and `create_host_path: false`.
- `compose.yaml`, `deploy/local.sh`, `.env.example`: plain-directory equivalents, following the
  existing `SANDBOX_HOST_INPUT_ROOT` wiring.
- `tests/test_deployment.py`: stubbed script execution covers bucket/region quoting, pinned
  installer idempotency, healthy unchanged mount preservation, changed configuration retirement,
  failed readiness, manual recovery ordering/abort, both retirement labels, wrong-volume and
  nonempty backing rejection, immutable reruns and cleanup failure. Static assertions cover
  Compose mappings, packaged assets and service dependencies. Shell syntax and Linux unit
  verification are recorded separately from live host tests below.
- `src/assistant_agent/sandbox_kit/` is not touched.

### Phase 8 — Docs

Status: Implemented; rollout and unperformed acceptance are explicitly documented.

- `docs/dev-plans/running-cli-inside-docker.md`: settings table and files/permissions section.
- `README.md`: sandbox section, the stale "instance role has no application S3 permissions" line
  (`:221`), and the accepted metadata risk.
- Document service installation/startup, readiness and recovery, cache/disk monitoring, and
  `systemctl status` / `journalctl` diagnostics. Explain that agents see normal files while
  the host handles S3 persistence, and record the final lifecycle decisions in this plan.

## Implemented lifecycle and verification evidence

The image packages two units plus the host helpers in the unchanged flat `/app/deploy`
directory. `deploy/production/bootstrap.sh` pulls images and extracts that bundle, then
invokes `apply.sh` to perform host deployment. `apply.sh` writes a
root-only `/etc/assistant-agent/storage.conf`, installs helpers under
`/usr/local/libexec/assistant-agent`, and enables storage and app startup.
`daily-logs-host.sh prepare` verifies the EBS UUID/ext4 filesystem, preserves cache contents,
rejects a remaining parent mount and protects only an empty uncovered mountpoint. `ready`
checks the exact FUSE type/source and runs the UID/GID 10001 probe; mount polling takes at
most 75 seconds and probe I/O has a 30-second deadline within the 180-second service startup
limit. `cleanup` refuses unexpected filesystems and uses a bounded non-lazy unmount.

`install-s3fs.sh` accepts only v1.95 linked against `libfuse.so.2`; it protects AL2023's
`curl-minimal` and `libcurl-minimal` packages during dependency resolution. The pinned source tag
archive is `https://codeload.github.com/s3fs-fuse/s3fs-fuse/tar.gz/refs/tags/v1.95`, SHA-256
`0c97b8922f005500d36f72aee29a1345c94191f61d795e2a7b79fb7e3e6f5517` (downloaded and checked
2026-10-12). Real AL2023 dependency resolution remains a rollout gate.

`app-host.sh check` waits for PostgreSQL and probes through the production app image, bind
and identity; `app-host.sh ready` probes the actual recreated app in `ExecStartPost`
before activation succeeds. Migrations run before `assistant-app.service` starts. Compose remains attached
to a force-recreated app and propagates its exit code; systemd supervises it. App shutdown
removes the app and transient app containers and verifies retirement of sandboxes
using both owner and deployment labels. `recover-daily-logs.sh` stops the app, retires remaining
sandboxes, stops/cleans storage, prepares/starts/probes it, then starts the app. Any failure
aborts the sequence. Repeated deployment probes but preserves a healthy unchanged mount;
an inactive unchanged mount requires explicit recovery. Configuration changes retire the old
app and mount before replacing configuration. No EC2 user-data changes or writer guard were added.

The list grant uses only `StringLike s3:prefix = "user-data/*"`, including `user-data/` but
excluding a bare `user-data` listing that could enumerate sibling names. Object grants cover
only current objects under `user-data/*`; bucket protections and versioning are unchanged.
References: [S3 prefix conditions](https://docs.aws.amazon.com/AmazonS3/latest/userguide/amazon-s3-policy-keys.html),
[AL2023 minimal curl](https://docs.aws.amazon.com/linux/al2023/ug/curl-minimal.html),
[Docker restart supervision](https://docs.docker.com/engine/containers/start-containers-automatically/).

Historical checks completed 2026-10-12 before the lifecycle simplification (not evidence
for the new lifecycle; see [simplification validation](simplify-s3fs.md)):

| Check | Result and limits |
| --- | --- |
| Regression and lint | 611 passed, 15 skipped; Ruff passed. Seven existing SQLite datetime deprecation warnings. Includes 24 deployment tests. |
| Shell and whitespace | All deployment shell scripts pass `bash -n`; `git diff --check` passed. |
| Terraform | `fmt -check` and `validate` passed. Existing `main.tf` alignment was formatted; no apply or live plan run. |
| Compose | Local and production configuration rendered with synthetic secrets and paths. |
| Unit syntax/dependencies | The previous three-unit configuration passed `systemd-analyze verify` in disposable Ubuntu/systemd 255 with executable placeholders for unavailable s3fs and Docker dependencies. No running systemd manager, boot or failure-propagation test. |
| Isolated local storage | Current DailyLogService created a file as UID 10001; UID 1000 read/wrote/fsynced it with two sandbox binds; replacement sandbox reread it after input cleanup and root reinitialization. OrbStack translates ownership, so this does not prove native Linux on-disk UID ownership. No authenticated conversation reset or billable Claude call. |
| Production | Not performed: Terraform apply/deployment, AL2023 build, FUSE/S3 access, native identity/protection checks, reboot and manual failure recovery. |

## Explicit production rollout (not performed by implementation)

1. On a disposable AL2023 systemd host, verify the pinned installer and dependencies without
   replacing `curl-minimal`; exercise service failure propagation and boot ordering.
   See [the simplification acceptance matrix](simplify-s3fs.md) for additional migration checks.
2. `terraform -chdir=terraform/main plan -out=/tmp/assistant-agent.tfplan`: review the intended prefix-scoped inline policy.
   The AMI is pinned through `ami_id`; stop if the plan proposes unexpected instance
   replacement and review the cause before approving infrastructure changes.
3. `terraform -chdir=terraform/main apply /tmp/assistant-agent.tfplan`.
4. `deploy/deploy.sh --setup-s3fs` for first setup; ordinary deployments omit the flag and
   never compile or install packages. Explicit setup allows an extended SSM
   execution/polling window. If polling expires, inspect the invocation status before retrying.
5. Complete the production checks below, including independent S3 readback, both runtime
   identities, reboot, manual recovery and fail-closed uncovered mountpoint behavior.

Operational commands and rollback guidance are in the
[Docker runtime runbook](running-cli-inside-docker.md#production-daily-log-storage).

## Verification

- **Static:** `uv run pytest -q`, `uv run ruff check .`, `for script in deploy/*.sh deploy/production/*.sh deploy/production/runtime/*.sh; do bash -n "$script" || break; done`,
  `terraform -chdir=terraform/main validate`.
- **Local:**
  1. `deploy/local.sh`, sign in, ask the agent to note something.
  2. `data/user-data/<user_id>/daily-logs/<today>.md` has the header and the note.
  3. `docker inspect` on the sandbox shows two mounts, `/daily-logs` read-write.
  4. Asking the agent to write under `/workspace` is denied.
  5. Reset the conversation and ask what was noted earlier.
- **Production:**
  1. `findmnt /srv/assistant-agent/s3/user-data` shows `fuse.s3fs`.
  2. Repeat the local flow, then `aws s3 ls s3://<bucket>/user-data/<user_id>/daily-logs/` shows
     `<today>.md` and `aws s3 cp ... -` prints the agent's text.
  3. Run the manual recovery helper, verify app and sandbox retirement, then reread the entry
     in a new conversation. Repeat startup verification across an instance reboot.
  4. Stop the service and unmount: the app stops and `touch` in the uncovered mountpoint fails
     even as root. Verify no fallback data is written to EBS.
  5. Fail the mount with live conversations, run manual recovery, and verify affected
     conversations are retired and new conversations can read the persisted daily files.

## Risks and open items

- **Accepted risk (follow-up):** once the instance role has S3 access, a sandbox that can reach
  instance metadata could read other users' prefixes.
- **Mount dropped mid-session:** retire affected conversations; their private binds cannot be
  repaired by a host remount. New conversations use the recovered mount.
- **Bash redirects:** an approved command such as `ls ... > /daily-logs/x` can write there; same
  authority as Edit.
- **Disconnect orphans logs:** disconnecting deletes the user row and reconnecting issues a new
  `user_id`, so old logs stay in S3 unreferenced. This plan leaves them in place.
- **Pending host acceptance:** exact AL2023 package/dependency installation, live scoped-IAM
  mount/probe behavior, direct FUSE binds and container recreation, immutable EBS protection, real systemd
  boot/failure propagation, and reboot/manual recovery with S3 readback. The app has an explicit
  tzdata dependency; isolated local daily-file preparation exercised UTC resolution.
