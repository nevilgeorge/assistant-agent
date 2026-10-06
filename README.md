# Gmail and Calendar connection app

FastAPI lets each user connect a Google account for **read-only** Gmail and Google Calendar access. The web app assigns each user an internal `user_id` and keeps Google's verified `sub` in a unique `google_sub` column. It encrypts refreshable credentials in PostgreSQL and keeps opaque browser sessions in the database. The browser receives only a secure, HTTP-only session cookie in production. Exports for web users are a later feature. Existing local files are never uploaded.

The legacy CLI (`assistant-agent accounts`, `verify`, `export-2026`) continues to use local `data/tokens.json` and export files. That store is separate from web users and is not migrated automatically.

Connected users share one live Claude conversation across their tabs and logins. The app
allocates a dedicated sandbox container on the first accepted prompt and keeps a non-TTY
Docker exec open for that conversation, sends
newline-delimited JSON prompts to stdin, and continuously reads structured stdout.
`POST /api/message` accepts a prompt with the session cookie and CSRF token and returns
`202` with conversation/turn IDs. `GET /api/conversation` returns the in-memory transcript;
`GET /api/conversation/stream?conversation_id=...&after=...` streams SSE text deltas and
turn events, with sequence-based replay. `POST /api/conversation/reset` starts fresh.
The browser uses plain text rendering and the **New conversation** button to reset.

Turns continue if the browser disconnects. Overlapping prompts return `409`; turns have
a two-minute deadline. Idle conversations expire after 30 minutes. All conversations
and context are lost on app restart; there is no disk persistence or automatic retry.
A failed CLI conversation requires a new conversation. Tools and MCP remain disabled, so
Claude cannot read or change workspace files. Token issuance, input publication, and file-download tools remain later phases.

Run exactly one app worker/replica per deployment: the conversation manager is process-local
and startup removes earlier containers belonging to that deployment. Configure `CHAT_MAX_SESSIONS`
(default `4`) and `CHAT_IDLE_SECONDS` (default `1800`) in the app environment. Each
conversation accepts up to 2 MiB of cumulative prompt/response text. SSE clients replay
up to 2048 recent events; clients behind that window reload the transcript snapshot.
Pending allocations, live containers, and failed container cleanup continue to consume
sandbox capacity until removal is confirmed. Allocation and readiness have a 30-second
budget, followed by a 15-second Claude startup budget.

Alembic revision `0002` assigns an internal `user_id` to existing web users, retains each unique `google_sub`, and updates existing sessions to reference the new key. Run `uv run --env-file .env alembic upgrade head` before starting the updated app locally. The deployment script runs migrations automatically on EC2.

Revision `0003` adds a UUID `id` primary key to web sessions and renames `id_hash` to `session_token_hash`, with a unique index for token lookups. Existing sessions remain valid after migration.

## Google setup

Enable Gmail API and Google Calendar API in Google Cloud. Create an OAuth **Web application** client and register exactly `http://localhost:8000/auth/google/callback` for local development or `https://YOUR_DOMAIN/auth/google/callback` for production. Request `openid`, `userinfo.email`, `gmail.readonly`, and `calendar.readonly`. The last two grant read access only. The callback validates OAuth state, PKCE, ID token nonce, verified email, Google `sub`, all requested scopes, and the presence of a refresh token.

Public launch requires [Google verification for the restricted Gmail scope](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification) and the applicable server-side security assessment. While the OAuth app is in Testing, Google may expire refresh tokens after seven days.

## Local development

Create `.env` from `.env.example`, set Google values, generate a Fernet key, and point `DATABASE_URL` to PostgreSQL. Then:

```bash
uv sync
uv run --env-file .env alembic upgrade head
uv run assistant-agent serve
```

For local Compose, create `deploy/local-db-password.txt` (ignored by Git), put the same password in `.env` as `POSTGRES_PASSWORD`, and set `ANTHROPIC_API_KEY` in `.env`. Start Docker, then run:

```bash
./deploy/local.sh
```

The script builds the app and tagged sandbox image, resolves the absolute host session
root to `<repo>/data/session-inputs`, initializes it as UID 10001 with mode `0700`, discovers
Docker socket permissions, runs database migrations, and waits for readiness. Run it again after source changes to rebuild the containers. At `http://localhost:8000`, sign in and use the **Ask the assistant** card. Claude stays running across turns and streams responses into the chat transcript; tools and disk persistence remain disabled.

The app listens on `127.0.0.1:8000` and PostgreSQL on `127.0.0.1:5432`. Dedicated sandboxes publish no ports and keep `/workspace` in their disposable container
layer. Only their conversation input directory is mounted at `/input:ro`. Local exports under `data/` do not enter the containers. The app mounts the Docker socket to execute commands in the sandbox, matching production; this grants control of the local Docker daemon.

Before running Compose diagnostics directly, export the same host path from the repo root:

```bash
export SANDBOX_HOST_INPUT_ROOT="$(pwd -P)/data/session-inputs"
```

Use `docker compose ps` to inspect services and `docker compose logs app` for diagnostics. Stop with `docker compose down`, which preserves database data. Ending a conversation discards its sandbox files. Set `DATABASE_URL` for the host if you run migrations outside Docker.

### Agent kit

Each export directory carries a `CLAUDE.md` explaining the format to an assistant
agent, plus the stdlib-only `build_index.py` that turns the export into a queryable
index. The source of truth for those files is `src/assistant_agent/agent_kit/`; the
copies under `data/` are generated. Install them after an export has created the
directories:

```bash
uv run assistant-agent export-2026
uv run assistant-agent install-kit
uv run assistant-agent install-kit --check   # report drift, write nothing
```

### Sandbox kit

`src/assistant_agent/sandbox_kit/` holds a vendored copy of the
[agent-sandbox](https://github.com/nevilgeorge/agent-sandbox) build context — the
`Dockerfile` for the container that runs Claude Code, its `.dockerignore`, and
`docker/entrypoint.sh`. That is the entire upstream context; nothing else in that repo
reaches a layer.

Despite the name, this is **not** an agent-kit-style installed kit. Nothing copies it
anywhere, there is no manifest, and `install-kit` does not touch it — `deploy/deploy.sh`
builds it straight from the working tree. The copies are byte-identical to upstream, so
`diff` is the drift check. See `src/assistant_agent/sandbox_kit/UPSTREAM.md` for the
pinned commit and the re-sync command.

Do not add a provenance header to that `Dockerfile`: its first line must stay
`# syntax=docker/dockerfile:1`, or BuildKit's frontend selection silently turns off and
the cache mounts stop working.

`install-kit` refuses to overwrite a copy that was edited in place and reports it
instead; pass `--force` once the change has been moved back into
`src/assistant_agent/agent_kit/`. Edit the packaged source, never the copy.

Run `build_index.py` from the export directory it was installed into, not from
`src/assistant_agent/agent_kit/` -- it writes its index beside itself, and `.index/`
and `*.jsonl` are gitignored precisely so a stray run cannot leave mail-derived
output in a tracked path.

To inspect the database, open an interactive `psql` session inside the database container:

```bash
docker compose exec db psql -U assistant_agent -d assistant_agent
```

Use `\dt` to list tables, `\d web_sessions` to inspect the session table's columns and indexes, `\x auto` for readable wide results, and `\q` to exit. SQL queries require a semicolon, for example `SELECT user_id, email FROM users LIMIT 20;`.

## AWS deployment

The target is one `t4g.medium` in `us-east-1`. PostgreSQL, Caddy, and the app run on Docker Compose; the app manages up to four
dedicated conversation sandboxes. An encrypted, retained EBS volume holds PostgreSQL
data, Caddy certificates, and downloaded session inputs. Sandbox workspaces are disposable. There is **no database backup** in this phase; loss of this volume would require users to reconnect. Terraform creates a VPC, one public and two reserved private subnets, Elastic IP, two ECR repositories, instance role, SSM access, and an instance-status alarm. Only 80 and 443 are open inbound. SSH is through [Session Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html), with no inbound SSH rule.

The root volume is 30 GiB rather than the AMI's 8 GiB default: it holds `/var/lib/docker`, and the sandbox image alone is roughly 1.9 GiB. `deploy/remote.sh` also creates a 2 GiB swap file there, because Amazon Linux 2023 ships without swap and 4 GiB of RAM leaves little slack once an agent is working.

1. Install Terraform, AWS CLI, and Docker Buildx. Configure AWS credentials locally.
2. Choose a globally unique state bucket name and run `terraform -chdir=terraform/bootstrap init`, then `terraform -chdir=terraform/bootstrap apply -var='state_bucket_name=YOUR_BUCKET'`. The bucket is versioned and encrypted.
3. Run `terraform -chdir=terraform/main init -backend-config='bucket=YOUR_BUCKET'`, then review `terraform -chdir=terraform/main plan` before running `terraform -chdir=terraform/main apply`. The [S3 backend](https://developer.hashicorp.com/terraform/language/backend/s3) uses S3 lockfiles. `terraform/main/terraform.tfvars` names the separate application-data bucket. Save the six Terraform outputs.
4. Point `assistant.nevilgeorge.me`'s A record to `elastic_ip`, create the production Google OAuth callback URL, and add the domain to the consent screen.
5. Run `uv run python deploy/put-secrets.py`. It prompts for Google client ID and secret and the Anthropic API key, and can generate the Fernet key and PostgreSQL password. Values are written directly to SSM SecureString, outside Terraform state. Do this before deploying: `deploy/remote.sh` aborts if a parameter is missing.
6. If the root volume was just resized, grow its filesystem before deploying — `terraform apply` resizes the block device but the instance does not reboot, so cloud-init's `growpart` never runs. Over Session Manager: `sudo growpart /dev/nvme0n1 1 && sudo xfs_growfs /`, then check `df -h /`. Skipping this can exhaust the old 8 GiB volume partway through pulling the 1.9 GiB sandbox image.
7. Run `deploy/deploy.sh`. It reads the instance ID, both ECR repository URLs, and data volume ID from the initialized `terraform/main` outputs and defaults the domain to `assistant.nevilgeorge.me`. You can override these values with `DOMAIN`, `INSTANCE_ID`, `ECR_REPOSITORY_URL`, `SANDBOX_ECR_REPOSITORY_URL`, and `DATA_VOLUME_ID` environment variables. It builds and pushes two Linux ARM64 images — the app and the agent sandbox, sharing one tag — sends a [Run Command](https://docs.aws.amazon.com/systems-manager/latest/userguide/run-command.html) to the instance, mounts the retained EBS volume, fetches secrets, runs Alembic, starts Compose, and checks HTTPS readiness. ECR authentication uses [AWS's registry login flow](https://docs.aws.amazon.com/AmazonECR/latest/userguide/registry_auth.html).

### Manual export upload

After Terraform creates the application-data bucket, run the following command from the checkout containing your local `data/` directory. A fresh Git worktree does not have `data/` because Git ignores it. The dry run lists the files that would be uploaded; review it, then run the same command without `--dryrun`.

```bash
aws s3 sync data/ \
  s3://assistant-agent-data-051638360892-staging/user-data/811add5cd1414fee9baa097c41d7bd41/ \
  --exclude '*' \
  --include 'emails/2026/*' \
  --include 'calendar/2026/*' \
  --include 'emails/CLAUDE.md' \
  --include 'emails/build_index.py' \
  --include 'emails/decode_email_bodies.py' \
  --include 'calendar/CLAUDE.md' \
  --include 'calendar/build_index.py' \
  --include 'export-2026*-report.json' \
  --include '.agent-kit.json' \
  --dryrun
```

The allowlist omits `tokens.json`, generated indexes, and caches. The sync does not delete objects already in S3. After uploading, check the destination with:

```bash
aws s3 ls s3://assistant-agent-data-051638360892-staging/user-data/811add5cd1414fee9baa097c41d7bd41/ --recursive --summarize
```

Bucket versioning retains overwritten and deleted object versions, which incur S3 storage charges. The CLI and application still use local `data/`; this upload does not change their storage behavior.

### The agent sandbox

The app creates one container per conversation and reuses it across turns. Reset,
disconnect, idle expiry, turn or transport failure, and shutdown retire the conversation,
terminate its process group, force-remove its container, and then delete its inputs.
Startup removes abandoned containers labeled for this deployment and generated session
directories. It never adopts an earlier container. Docker outages or incomplete cleanup
leave the web app available but block new allocation until reconciliation succeeds.

Each sandbox runs as UID 1000 with init, all capabilities dropped,
`no-new-privileges`, PID limit 2048, 2 GiB memory, one CPU, bounded Docker logs, and
restart policy `no`. It has no published ports or Docker socket. Four slots deliberately
allow memory overcommit on the 4 GiB production host. Images are built/pulled during
deployment, never during chat requests. The vendored sandbox image remains unchanged.

Configure `SANDBOX_IMAGE`, `SANDBOX_DEPLOYMENT_ID`, `SANDBOX_NETWORK`,
`SANDBOX_HOST_INPUT_ROOT`, and `SANDBOX_APP_INPUT_ROOT`. Docker bind sources use
the daemon-host root, while the app creates directories through the app-visible root.
Production uses `/srv/assistant-agent/session-inputs` for both; local deployment mounts the
absolute `<repo>/data/session-inputs` there. The root is owned by app UID 10001 with mode `0700`. Each conversation input
directory, `session-inputs/<conversation-id>`, is owned by the app with mode `0755`.
Future published input files must use `0644`, and nested directories `0755`, so sandbox
UID 1000 can read them through `/input:ro`. These are downloaded inputs, not saved
conversations. Container deletion discards `/workspace`, copied inputs, CLI state, and
processes. Deployment leaves the old shared workspace files unused.

The app mounts `/var/run/docker.sock` and receives the daemon's group via `group_add`.
**That socket grants root-equivalent control of the Docker host.** Sandboxes attach only
to an explicitly named bridge, where the app is reachable as `app`. Backend services
stay on the Compose default network. The bridge still exposes all app routes and is
not a complete network isolation boundary; sandbox egress remains unrestricted.
Google credentials stay in the app. The Anthropic key is supplied only to the Claude
exec environment and is absent from sandbox-wide configuration.

```bash
uv run assistant-agent sandbox                              # daemon/image/network readiness
uv run assistant-agent sandbox --container CONTAINER_ID claude --version
```

After deployment, verify the page over HTTPS, complete Google's consent for both scopes, disconnect and reconnect, then restart the app and instance to confirm the database and Caddy data survive, while old conversation sandboxes are removed. The domain and Google verification are external prerequisites; they are not supplied by this repository. The instance role has no application S3 permissions. Add them only when storage is implemented. A future managed PostgreSQL migration can use the reserved private subnets.

## Checks

```bash
uv run pytest -q
DATABASE_URL=sqlite:////tmp/assistant-agent-migration.db uv run alembic upgrade head
SANDBOX_HOST_INPUT_ROOT="$PWD/data/session-inputs" docker compose config --quiet
DOMAIN=example.com IMAGE_URI=example.invalid/app:test SANDBOX_IMAGE_URI=example.invalid/sandbox:test DOCKER_GID=991 POSTGRES_PASSWORD=test GOOGLE_CLIENT_ID=test GOOGLE_CLIENT_SECRET=test CREDENTIAL_ENCRYPTION_KEY=test ANTHROPIC_API_KEY=test docker compose -f deploy/compose.prod.yaml config --quiet
terraform -chdir=terraform/main validate
bash -n deploy/local.sh deploy/remote.sh deploy/deploy.sh
docker buildx build --platform linux/arm64 -f src/assistant_agent/sandbox_kit/Dockerfile src/assistant_agent/sandbox_kit
```

### Async web runtime

FastAPI routes await SQLAlchemy database operations. The app creates its engine and
store during lifespan startup and disposes the engine during shutdown, including
failed startup. `create_app()` provides a fresh app for tests; the deployment entry
point remains `assistant_agent.web:app`.

Keep `DATABASE_URL=postgresql+psycopg://...` for both the web app and synchronous
Alembic migrations. The web engine selects psycopg's async dialect. There is no
schema migration for this change. Web tests use `sqlite+aiosqlite://`; synchronous
migration tests continue to use `sqlite://`.

Google SDK network calls run in bounded AnyIO workers (four concurrent calls), with
10-second network timeouts. Database sessions are closed before waiting for Google
or chat operations. Credential refresh updates existing users only, so a late
refresh cannot recreate a disconnected account. Chat conversations and Docker exec use
asyncio and aiodocker on the app event loop. The lifespan owns separate Docker
clients for control commands and interactive attachments, and joins chat work
before closing them. Run one app worker per deployment. The CLI exporter remains
synchronous.

Run the optional PostgreSQL check against a development database:

```bash
ASYNC_TEST_DATABASE_URL='postgresql+psycopg://user:password@localhost:5432/database' uv run pytest -q tests/test_web_auth.py -k postgres_async
```

The check creates and drops a temporary schema and does not modify application
tables. It verifies the async driver, transaction rollback, concurrent queries,
and pool disposal. Normal tests also cover event-loop progress during slow I/O,
transaction cancellation, SSE authentication expiry, and lifespan cleanup failures.

### Live chat integration check

With Docker running and the configured sandbox image already built, verify dedicated
container lifecycle and streaming transport without model calls:

```bash
SANDBOX_DOCKER_INTEGRATION=1 uv run pytest -q tests/test_sandbox_async.py
```

This checks repeated writes on one exec, separate stdout/stderr, exit codes,
conversation reuse, and process group termination including descendants. The
application uses aiodocker's public stream API; its protocol and transcript limits
remain 2 MiB. Docker frame allocation belongs to aiodocker, which does not impose
the old application's 16 MiB frame header limit.

For the optional two-turn model check, configure the app Anthropic key and sandbox
settings, then run:

```bash
CHAT_DOCKER_INTEGRATION=1 uv run pytest -q tests/test_chat.py -k real_claude
```

This makes real model requests. Normal tests use a fake Claude process and do not
require Docker or provider credentials. Production SSE/proxy behavior must also be
checked after deploying: confirm incremental text, reload/reconnect, and safe plain-text
rendering.
