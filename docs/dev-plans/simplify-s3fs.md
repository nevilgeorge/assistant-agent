# Simplify the s3fs lifecycle

Keep the existing S3 bucket, `user-data/<user_id>/daily-logs/` layout, permissions,
cache, and explicit recovery policy. The separate deployment-folder reorganization
is described in the [deployment guide](../../deploy/README.md); runtime behavior
and installed paths remain unchanged.

## Lifecycle

Two enabled services own startup: `assistant-daily-logs.service` and
`assistant-app.service`. Storage runs guarded cleanup and preparation in ordered
`ExecStartPre` commands, foreground s3fs, then a bounded readiness probe. Activation
allows 180 seconds. The app retains `Requires`, `After`, and `BindsTo` dependencies
and probes storage through its container bind. Storage automatic restart and Compose
app restart remain disabled; systemd supervises app failures.

Compose directly binds `/srv/assistant-agent/s3/user-data` at the same path with
private propagation and `create_host_path: false`. There is no shared-parent mount
or propagation check. Existing per-user sandbox binds remain. App startup always
force-recreates only the app container; database and Caddy containers are excluded.

Storage replacement and the existing recovery command follow one sequence:

1. Stop/remove the app and transient app containers; retire every sandbox matching
   both application-owner and deployment labels, even if the app unit is inactive.
   Verify retirement before proceeding.
2. Stop storage and verify ordinary bounded unmounting. Never use lazy unmount or
   discard the cache to conceal failure.
3. Start storage, including preparation and readiness.
4. Recreate the app and verify storage access through its bind.

Abort on any failure and leave the app stopped, canceling app restart attempts if
startup fails. Recovery ends current conversations. Ordinary deployment preserves
healthy unchanged storage and does not recover failed unchanged mounts.

## Explicit host setup

`install-s3fs.sh check [binary]` performs no package installation or network access.
It requires v1.95, native executable architecture, all required shared libraries,
and FUSE 2 linkage. Ordinary deployment checks before stopping consumers and reports
`--setup-s3fs` when repair is required.

`./deploy/deploy.sh --setup-s3fs` forwards explicit setup intent to the host.
`install-s3fs.sh install [destination]` exits immediately for a compatible binary;
otherwise it builds pinned source with limited parallelism and protects AL2023
`curl-minimal`/`libcurl-minimal`. No repository-package fallback or package-erasure
override is used. Replacement binaries are built and validated in staging before
downtime and installed only after the old mount stops.

Source: `https://codeload.github.com/s3fs-fuse/s3fs-fuse/tar.gz/refs/tags/v1.95`.
SHA-256: `0c97b8922f005500d36f72aee29a1345c94191f61d795e2a7b79fb7e3e6f5517`.

## Migration and rollback

Fresh installation and migration from three units use the same entrypoint. Keep old
helpers/configuration until consumers and the old FUSE mount are fully retired.
Before removing the legacy parent self-bind, verify the expected EBS UUID, ext4 type,
and `/s3` filesystem root; refuse unexpected mounts. Disable/remove the obsolete
preparation unit, install matching assets, reload systemd, and start storage before
consumers. Already-removed legacy resources are accepted on an interrupted rerun.
A root-owned `storage.conf.install-pending` marker survives interrupted replacement
and is removed only after storage readiness succeeds. This lets replacement resume
without turning an ordinary unchanged deployment into implicit recovery.

Rollback stops/removes consumers and storage before restoring the prior matching
units, helpers, and Compose configuration. Preserve the cache and S3 data throughout.
See the [storage runbook](running-cli-inside-docker.md#production-daily-log-storage).

## Verification and rollout gate

Command-stub tests verify script ordering, failure handling, installation boundaries,
asset packaging, and isolation. They cannot establish real systemd ordering, FUSE
mount behavior, native Linux permissions, or persistence. Disposable Linux lifecycle
acceptance is still required before production rollout:

| Area | Required checks |
| --- | --- |
| Installation | Compatible binary is a no-op; check never installs/downloads; incompatible binary fails before downtime; setup validates checksum/runtime dependencies and preserves minimal curl. |
| Lifecycle | First start, unchanged redeploy, app crash/restart, planned reboot, and successful recovery; exactly two enabled services and no parent self-bind. |
| Migration | Existing shared parent, fresh host, interrupted rerun, and refusal of unexpected parent mounts. |
| Failures | Missing/wrong EBS, absent/wrong S3 mount, nonempty uncovered directory, failed retirement/unmount, probe timeout, app startup failure all prevent unsafe startup. |
| Isolation | Both sandbox labels required; unrelated containers, cache, and persistent data survive. |
| Persistence | App and sandbox identities read/edit logs; independently verify resulting S3 objects. |

Live lifecycle acceptance was deferred at the user’s request on 2026-10-12.
Production troubleshooting on 2026-10-12 confirmed the pinned AL2023 build and
successful storage/app startup after adding explicit `subtype=s3fs`: FUSE 2 suppresses
its default subtype when `fsname` is set. The host mount, storage I/O probe, actual
app-container probe, database migration, and HTTPS health check passed. Deployment
also required closing a ten-day-old idle `psql` reader that blocked the migration;
the close was guarded against write transactions and write locks.

These startup checks do not establish the full acceptance matrix above. Disposable
Linux failure injection, reboot, and recovery acceptance remain deferred.
