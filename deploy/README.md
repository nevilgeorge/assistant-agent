# Deployment

Run operator commands from the repository root:

| Command | Purpose |
| --- | --- |
| `./deploy/local.sh` | Build and start the local app after configuring `.env` and `deploy/local-db-password.txt`. |
| `uv run python deploy/put-secrets.py` | Prompt for production secrets and write them to SSM; accepts `--prefix` and `--region`. |
| `./deploy/deploy.sh --setup-s3fs` | Deploy with explicit permission to build a missing or incompatible pinned s3fs binary. Use for initial setup or deliberate repair. |
| `./deploy/deploy.sh` | Build and push both images, then deploy through SSM. Check the existing s3fs installation before stopping the app. |

Provision [Terraform infrastructure](../terraform/) and configure production secrets
before deployment. The deployment command reads Terraform outputs unless overridden
by `INSTANCE_ID`, `ECR_REPOSITORY_URL`, `SANDBOX_ECR_REPOSITORY_URL`, `DATA_VOLUME_ID`,
or `APPLICATION_DATA_BUCKET_NAME`. `AWS_REGION` defaults to `us-east-1` and `DOMAIN`
defaults to `assistant.nevilgeorge.me`.

Local startup continues to use the repository's `compose.yaml` and ignored
`deploy/local-db-password.txt`. The password file is excluded from the Docker build
context; keep it local.

## Production ownership

`deploy.sh` embeds `production/bootstrap.sh` in the SSM command. Bootstrap is
self-contained: it authenticates to ECR, pulls both images, extracts the app image's
deployment bundle, and invokes `apply.sh` from that bundle. No repository checkout is
needed on the host.

`production/apply.sh` owns the host deployment sequence: validate and prepare EBS,
persistent directories, and swap; stage storage configuration and any explicit s3fs
installation; retire consumers and replace storage only when required; publish host
assets and establish readiness; then configure Compose, prepare PostgreSQL, run
migrations, start the app and Caddy, and verify HTTPS readiness. Old helpers and
configuration remain available until consumer and mount retirement completes.

`production/runtime/` holds the app and storage helpers, readiness probe, and manual
recovery command. `production/systemd/` holds the two service units. Compose, Caddy,
and the pinned installer live directly under `production/`. Docker packaging lists
assets explicitly and flattens them into `/app/deploy`; installed helpers remain in
`/usr/local/libexec/assistant-agent`, configuration in `/etc/assistant-agent`, and
Compose files in `/opt/assistant-agent`.

Systemd owns storage and app startup. Each app start recreates its container against
the ready mount. Ordinary deployment preserves healthy unchanged storage; it does
not recover failed unchanged storage. Recovery ends active conversations and uses:

```sh
sudo /usr/local/libexec/assistant-agent/recover-daily-logs.sh
```

See the [storage runbook](../docs/dev-plans/running-cli-inside-docker.md#production-daily-log-storage)
for diagnostics, recovery, migration, and rollback, and the
[s3fs lifecycle plan](../docs/dev-plans/simplify-s3fs.md) for acceptance criteria.
Live Linux systemd/FUSE acceptance remains deferred; repository checks do not replace
those production rollout checks.
