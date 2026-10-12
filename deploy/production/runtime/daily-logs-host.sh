#!/bin/bash
# Root-only host preparation and narrowly scoped FUSE cleanup. Never deletes stored data.
set -euo pipefail
source "${ASSISTANT_STORAGE_CONFIG:-/etc/assistant-agent/storage.conf}"
: "${DATA_ROOT:?}" "${DATA_VOLUME_UUID:?}" "${APPLICATION_DATA_BUCKET_NAME:?}"
parent="$DATA_ROOT/s3"
mountpoint="$parent/user-data"
cache="$DATA_ROOT/s3fs-cache"
helpers=${ASSISTANT_HELPER_DIR:-/usr/local/libexec/assistant-agent}
fail() { echo "daily-logs: $*" >&2; exit 1; }
mount_type() { findmnt -rn --mountpoint "$mountpoint" -o FSTYPE; }
verify_mount() {
  # A filesystem at this path is insufficient: it must serve the configured S3 prefix.
  # The service sets fsname explicitly so SOURCE can be checked consistently.
  [[ $(mount_type) == fuse.s3fs ]] &&
    [[ $(findmnt -rn --mountpoint "$mountpoint" -o SOURCE) == "$APPLICATION_DATA_BUCKET_NAME:/user-data" ]]
}
case "${1:-}" in
  prepare)
    # Check the actual mount before creating anything, avoiding writes to the root disk
    # when EBS is missing. Symlinks must not redirect privileged setup elsewhere.
    [[ $(findmnt -rn --mountpoint "$DATA_ROOT" -o UUID) == "$DATA_VOLUME_UUID" ]] || fail 'missing or wrong EBS mount'
    [[ $(findmnt -rn --mountpoint "$DATA_ROOT" -o FSTYPE) == ext4 ]] || fail 'unexpected EBS filesystem'
    [[ ! -L "$DATA_ROOT" && ! -L "$parent" && ! -L "$cache" && ! -L "$mountpoint" ]] || fail 'symlink in storage paths'
    install -d -m 0700 -o root -g root "$cache"
    mkdir -p "$parent"
    # Parent self-binds belong to the old lifecycle and must be retired by migration.
    ! findmnt -rn --mountpoint "$parent" >/dev/null || fail 'unexpected parent mount; migrate before starting storage'
    if findmnt -rn --mountpoint "$mountpoint" >/dev/null; then
      verify_mount || fail 'unexpected user-data filesystem'
    else
      mkdir -p "$mountpoint"
      # Never hide existing backing data beneath FUSE. Protect the empty directory
      # so even root/Docker cannot silently write to EBS when the S3 mount is absent.
      [[ -z $(find "$mountpoint" -mindepth 1 -maxdepth 1 -print -quit) ]] || fail 'nonempty uncovered user-data directory'
      if lsattr -d "$mountpoint" | awk '{print $1}' | grep -q i; then
        # Immutable inodes reject metadata changes; validate rather than reapply them.
        [[ $(stat -c '%u:%g:%a' "$mountpoint") == 0:0:0 ]] || fail 'protected mountpoint has unexpected metadata'
      else
        chown root:root "$mountpoint"
        chmod 000 "$mountpoint"
        chattr +i "$mountpoint"
      fi
      lsattr -d "$mountpoint" | awk '{print $1}' | grep -q i || fail 'immutable protection missing'
    fi
    ;;
  ready)
    # Type=exec precedes FUSE mount completion. Bound mount polling separately from I/O.
    # Allow 75 seconds for mounting plus the probe's 30 seconds within the unit's
    # 180-second startup limit. Drop root and supplementary groups before probing.
    for attempt in $(seq 1 75); do
      if verify_mount; then
        exec setpriv --reuid=10001 --regid=10001 --clear-groups \
          python3 "$helpers/daily-logs-probe.py" "$mountpoint" "$APPLICATION_DATA_BUCKET_NAME:/user-data"
      fi
      sleep 1
    done
    fail 'mount readiness deadline'
    ;;
  cleanup)
    if findmnt -rn --mountpoint "$mountpoint" >/dev/null; then
      verify_mount || fail 'refusing cleanup of unexpected filesystem'
    fi
    # Also check an absent host mount: consumers can retain detached private binds.
    "$helpers/app-host.sh" retired || fail 'storage consumers remain; retire them before cleanup'
    if findmnt -rn --mountpoint "$mountpoint" >/dev/null; then
      # No lazy unmount: dangling private binds must be retired before recovery.
      timeout --kill-after=2 15 fusermount -u "$mountpoint" || fail 'unmount failed; app must remain stopped'
      # A successful unmount command alone is not proof that the mount disappeared.
      ! findmnt -rn --mountpoint "$mountpoint" >/dev/null || fail 'mount remains after cleanup'
    fi
    ;;
  *) fail 'expected prepare, ready, or cleanup' ;;
esac
