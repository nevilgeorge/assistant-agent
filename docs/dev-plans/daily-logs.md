# Daily agent log files on an S3-backed FUSE mount

Status: Phases 1–6 implemented; Phases 7–8 pending. Last updated: 2026-10-12

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
- The app container binds the **parent** `/srv/assistant-agent/s3` with `rslave` propagation, so an
  s3fs remount stays visible without restarting the app. Binding the mountpoint itself would not
  survive a remount.
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
  Check package availability; otherwise `remote.sh` builds the release tarball once, guarded
  by a version check and a SHA-256 check computed at implementation time.
- **Single mount owner:** systemd owns startup and restart. Do not also install an fstab entry
  for this mount or independently mount it from deployment.

Track `deploy/assistant-daily-logs.service` as a plain-text systemd unit, not XML. Bundle it in
the app image alongside Compose and Caddy configuration. `deploy/remote.sh` extracts it,
substitutes the bucket and region from deployment configuration, and installs it as
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
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

This is the launch configuration, not the complete recovery implementation: add bounded
mount-readiness checks and stale-mount cleanup before restart. `Type=exec` confirms that the
executable launched, not that the mount is ready.

| Parameters | Purpose |
| --- | --- |
| `iam_role=auto` | Use the EC2 instance role without storing static credentials. |
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
  Configure and verify host mount propagation so the app's `rslave` bind receives remounts.
- After installing the unit, run `systemctl daemon-reload` and
  `systemctl enable --now assistant-daily-logs.service`. Enable starts it on future boots;
  network and data-volume dependencies order startup. Avoid restarting a healthy mount on
  every deployment; apply configuration changes with conversation retirement when needed.
- Before admitting conversations, verify the mount and perform a bounded application-owned
  write/read/delete probe. A successful `stat` alone does not prove write access.
- Restart a failed process with backoff and clean up any disconnected FUSE mount before
  remounting. Do not silently use the underlying EBS directory when the mount is unavailable.
- After a remount, retire affected sandbox conversations: their private bind mounts are not
  repaired by restarting s3fs. New conversations use the recovered host mount.
- Monitor service failures, filesystem errors, cache/disk space, and a periodic storage probe.
  A living process does not guarantee healthy storage. Inspect with
  `systemctl status assistant-daily-logs.service` and
  `journalctl -u assistant-daily-logs.service -f`.
- Before implementing the full feature, prove actual Claude edits, S3 readback, reset/reread,
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

- `terraform/main/application-data.tf`: `aws_iam_role_policy` on the instance role with
  `s3:ListBucket` limited to the `user-data` prefix, and `s3:GetObject`, `s3:PutObject`,
  `s3:DeleteObject`, `s3:AbortMultipartUpload` on `<bucket>/user-data/*`. No
  `DeleteObjectVersion`, so bucket versioning keeps a recoverable history of every overwrite.
- `deploy/deploy.sh`: read the `application_data_bucket_name` output and pass it and the
  deployment region to `remote.sh` for rendering the service configuration.
- `deploy/assistant-daily-logs.service` and `Dockerfile`: track and bundle the service unit
  alongside the existing deployment configuration. Use the command/options from
  "s3fs-fuse mount details": `Type=exec`, root execution, s3fs `-f`, instance-role credentials,
  HTTPS region endpoint, UID/GID/masks, cache settings, timeouts/retries, warning logging,
  `Restart=on-failure`, and `RestartSec=5`. Order startup after networking and the data volume.
- `deploy/remote.sh` (after the `session-inputs` block): install s3fs if the pinned version is
  missing; create the `0700` cache directory; create and `chattr +i` the mountpoint; configure
  host propagation; extract, render, and install the systemd unit; reload systemd and enable/start
  the service. Systemd is already present on AL2023; do not install it or add a competing fstab
  entry. Avoid restarting a healthy unchanged service on every deployment; retire affected
  conversations when applying mount configuration changes.
- Add the host readiness and stale-mount cleanup helpers needed by the service. A successful
  process launch or `stat` is insufficient: require a bounded mount and application-owned
  write/read/delete probe, and exit before app startup if storage readiness fails.
- `deploy/compose.prod.yaml`: both env vars set to `/srv/assistant-agent/s3/user-data`; long-syntax
  bind of `/srv/assistant-agent/s3` with `propagation: rslave` and `create_host_path: false`.
- `compose.yaml`, `deploy/local.sh`, `.env.example`: plain-directory equivalents, following the
  existing `SANDBOX_HOST_INPUT_ROOT` wiring.
- `tests/test_sandbox_kit.py`: add assertions for the new compose lines (near `:138`) and
  bundling of the service and any helper files. Validate shell syntax and the rendered unit
  with `systemd-analyze verify` on a systemd-capable host.
- `src/assistant_agent/sandbox_kit/` is not touched.

### Phase 8 — Docs

- `docs/dev-plans/running-cli-inside-docker.md`: settings table and files/permissions section.
- `README.md`: sandbox section, the stale "instance role has no application S3 permissions" line
  (`:221`), and the accepted metadata risk.
- Document service installation/startup, readiness and recovery, cache/disk monitoring, and
  `systemctl status` / `journalctl` diagnostics. Explain that agents see normal files while
  the host handles S3 persistence, and record the final lifecycle decisions in this plan.

## Manual steps (run by the user; no AWS CLI in this environment)

1. On the instance: `dnf info s3fs-fuse` (if it exists at a suitable version, use it and drop the
   source build) and confirm installing `libcurl-devel` does not replace `curl-minimal`.
2. `terraform -chdir=terraform/main plan`: expect one added policy. If it also wants to replace
   `aws_instance.app` (the AMI tracks latest), apply with a `-target` on the new policy.
3. `terraform apply`.
4. `deploy/deploy.sh`. The first run compiles s3fs and may outlast the SSM waiter; check the
   invocation result before assuming failure.

## Verification

- **Static:** `uv run pytest -q`, `uv run ruff check .`, `bash -n deploy/*.sh`,
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
  3. Restart `assistant-daily-logs.service` with no live conversations: the app still lists
     the directory without an app restart. Repeat across an instance reboot.
  4. Stop the service and unmount: `touch` in the mountpoint fails and a new chat returns 503.
  5. Fail the mount with live conversations, recover through systemd, and verify affected
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
- **Unverified until implementation:** s3fs package availability on AL2023, tz data in the app
  image, the exact S3 calls s3fs needs at mount time, and Docker's handling of a FUSE bind source.
