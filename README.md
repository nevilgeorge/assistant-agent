# Gmail and Calendar connection app

FastAPI lets each user connect a Google account for **read-only** Gmail and Google Calendar access. The web app assigns each user an internal `user_id` and keeps Google's verified `sub` in a unique `google_sub` column. It encrypts refreshable credentials in PostgreSQL and keeps opaque browser sessions in the database. The browser receives only a secure, HTTP-only session cookie in production. Exports for web users are a later feature. Existing local files are never uploaded.

The legacy CLI (`assistant-agent accounts`, `verify`, `export-2026`) continues to use local `data/tokens.json` and export files. That store is separate from web users and is not migrated automatically.

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

For local Compose, create `deploy/local-db-password.txt` (ignored by Git), put the same password in `.env` as `POSTGRES_PASSWORD`, set `DATABASE_URL` for the host if you use CLI migrations, and run:

```bash
docker compose up -d db
docker compose run --rm app alembic upgrade head
docker compose up -d app
```

The local Compose setup exposes only the app on `127.0.0.1:8000`. Local exports remain on disk under `data/` and do not enter the containers.

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

The target is one `t4g.medium` in `us-east-1`. PostgreSQL and Caddy run with the app on Docker Compose. An encrypted, retained EBS volume holds PostgreSQL data and Caddy certificates. There is **no database backup** in this phase; loss of this volume would require users to reconnect. Terraform creates a VPC, one public and two reserved private subnets, Elastic IP, ECR repository, instance role, SSM access, and an instance-status alarm. Only 80 and 443 are open inbound. SSH is through [Session Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html), with no inbound SSH rule.

1. Install Terraform, AWS CLI, and Docker Buildx. Configure AWS credentials locally.
2. Choose a globally unique state bucket name and run `terraform -chdir=terraform/bootstrap init`, then `terraform -chdir=terraform/bootstrap apply -var='state_bucket_name=YOUR_BUCKET'`. The bucket is versioned and encrypted.
3. Run `terraform -chdir=terraform/main init -backend-config='bucket=YOUR_BUCKET'` and `terraform -chdir=terraform/main apply`. The [S3 backend](https://developer.hashicorp.com/terraform/language/backend/s3) uses S3 lockfiles. Save the four Terraform outputs.
4. Point the chosen domain's A record to `elastic_ip`, create the production Google OAuth callback URL, and add the domain to the consent screen.
5. Run `uv run python deploy/put-secrets.py`. It prompts for Google client ID and secret and can generate the Fernet key and PostgreSQL password. Values are written directly to SSM SecureString, outside Terraform state.
6. Export `DOMAIN`, `INSTANCE_ID`, `ECR_REPOSITORY_URL`, and `DATA_VOLUME_ID` from Terraform outputs; then run `deploy/deploy.sh`. It builds and pushes a Linux ARM64 image, sends a [Run Command](https://docs.aws.amazon.com/systems-manager/latest/userguide/run-command.html) to the instance, mounts the retained EBS volume, fetches secrets, runs Alembic, starts Compose, and checks HTTPS readiness. ECR authentication uses [AWS's registry login flow](https://docs.aws.amazon.com/AmazonECR/latest/userguide/registry_auth.html).

After deployment, verify the page over HTTPS, complete Google's consent for both scopes, disconnect and reconnect, then restart the app and instance to confirm the database and Caddy data persist. The domain and Google verification are external prerequisites; they are not supplied by this repository. The instance role has no application S3 permissions. Add them only when storage is implemented. A future managed PostgreSQL migration can use the reserved private subnets.

## Checks

```bash
uv run pytest -q
DATABASE_URL=sqlite:////tmp/assistant-agent-migration.db uv run alembic upgrade head
DOMAIN=example.com IMAGE_URI=example.invalid/app:test POSTGRES_PASSWORD=test GOOGLE_CLIENT_ID=test GOOGLE_CLIENT_SECRET=test CREDENTIAL_ENCRYPTION_KEY=test docker compose -f deploy/compose.prod.yaml config --quiet
terraform -chdir=terraform/main validate
```
