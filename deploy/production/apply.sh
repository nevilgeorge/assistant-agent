#!/bin/bash
# Apply an extracted deployment bundle on the host. Bootstrap owns image transport;
# this script owns host preparation and the ordered transition to the new release.
set -euo pipefail

initialize_deployment() {
  assets=${1:?directory containing extracted deployment assets}
  work=${ASSISTANT_WORK_DIR:-/opt/assistant-agent}
  helpers=${ASSISTANT_HELPER_DIR:-/usr/local/libexec/assistant-agent}
  units=${ASSISTANT_UNIT_DIR:-/etc/systemd/system}
  config=${ASSISTANT_STORAGE_CONFIG:-/etc/assistant-agent/storage.conf}
  pending="$config.install-pending"
  legacy_unit="$units/assistant-daily-logs-prepare.service"
  export SETUP_S3FS=${SETUP_S3FS:-false}
  export SANDBOX_DEPLOYMENT_ID=${SANDBOX_DEPLOYMENT_ID:-production}
}

prepare_host() {
  mkdir -p "$work" /srv/assistant-agent
  volume_serial="${DATA_VOLUME_ID//-/}"
  for attempt in $(seq 1 60); do
    device=$(lsblk -ndo NAME,SERIAL | awk -v serial="$volume_serial" '$2 == serial {print "/dev/" $1; exit}')
    if [ -n "$device" ]; then break; fi
    sleep 2
  done
  [ -n "${device:-}" ] || { echo "Data volume is unavailable"; exit 1; }
  if ! blkid "$device" >/dev/null 2>&1; then mkfs.ext4 "$device"; fi
  uuid=$(blkid -s UUID -o value "$device")
  if ! mountpoint -q /srv/assistant-agent; then mount "$device" /srv/assistant-agent; fi
  [[ $(findmnt -rn --mountpoint /srv/assistant-agent -o UUID) == "$uuid" ]] || {
    echo "Wrong volume mounted at /srv/assistant-agent" >&2; exit 1;
  }
  [[ $(findmnt -rn --mountpoint /srv/assistant-agent -o FSTYPE) == ext4 ]] || {
    echo "Expected ext4 data volume" >&2; exit 1;
  }
  if ! grep -q "UUID=$uuid " /etc/fstab; then echo "UUID=$uuid /srv/assistant-agent ext4 defaults,nofail 0 2" >> /etc/fstab; fi
  mkdir -p /srv/assistant-agent/postgres /srv/assistant-agent/caddy-data /srv/assistant-agent/caddy-config
  chmod 700 /srv/assistant-agent/postgres /srv/assistant-agent/caddy-data
  # Session storage holds downloaded input, not persistent conversation workspaces.
  mkdir -p /srv/assistant-agent/session-inputs
  chown 10001:10001 /srv/assistant-agent/session-inputs
  chmod 700 /srv/assistant-agent/session-inputs
  # Amazon Linux 2023 ships without swap. On a 4 GiB instance an agent's memory spike would
  # otherwise leave the kernel to pick an OOM victim, and PostgreSQL is a plausible pick.
  # The file lives on the root volume, not /srv/assistant-agent: that mount is `nofail`, so
  # swap placed there would silently fail to activate on a boot without the data volume.
  if [ ! -f /swapfile ]; then
    dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
  fi
  swapon --show=NAME --noheadings | grep -qx /swapfile || swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw,nofail 0 0' >> /etc/fstab
  export DATA_VOLUME_UUID="$uuid" APPLICATION_DATA_BUCKET_NAME
}

prepare_candidates() {
  : "${APPLICATION_DATA_BUCKET_NAME:?}" "${AWS_REGION:?}" "${DATA_VOLUME_UUID:?}" "${candidate:?}"
  # These values are interpolated into a systemd command and sed substitutions.
  # Restrict their syntax before rendering; shell configuration is quoted with %q below.
  [[ "$APPLICATION_DATA_BUCKET_NAME" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || exit 1
  [[ "$AWS_REGION" =~ ^[a-z]{2}(-[a-z]+)+-[0-9]+$ ]] || exit 1
  [[ "$DATA_VOLUME_UUID" =~ ^[a-fA-F0-9-]+$ ]] || exit 1
  [[ "$SANDBOX_DEPLOYMENT_ID" =~ ^[a-zA-Z0-9_.-]+$ ]] || exit 1
  # Render candidates separately so comparison cannot overwrite the live configuration.
  printf 'DATA_ROOT=%q\nDATA_VOLUME_UUID=%q\nAPPLICATION_DATA_BUCKET_NAME=%q\nAWS_REGION=%q\nSANDBOX_DEPLOYMENT_ID=%q\n' \
    /srv/assistant-agent "$DATA_VOLUME_UUID" "$APPLICATION_DATA_BUCKET_NAME" "$AWS_REGION" "$SANDBOX_DEPLOYMENT_ID" > "$candidate/storage.conf"
  sed -e "s/@BUCKET@/$APPLICATION_DATA_BUCKET_NAME/g" -e "s/@REGION@/$AWS_REGION/g" \
    "$assets/assistant-daily-logs.service" > "$candidate/assistant-daily-logs.service"
  first_install=false
  [[ -f "$config" ]] || first_install=true
  mount_changed=false
  # Changes to mount units, readiness logic or storage settings require retirement.
  # App-only updates still stop the app, but preserve a healthy unchanged FUSE mount.
  cmp -s "$candidate/assistant-daily-logs.service" "$units/assistant-daily-logs.service" || mount_changed=true
  for file in daily-logs-host.sh daily-logs-probe.py; do
    cmp -s "$assets/$file" "$helpers/$file" || mount_changed=true
  done
  cmp -s "$candidate/storage.conf" "$config" || mount_changed=true
  # Ordinary deployments only validate. Explicit setup builds into staging while the
  # current mount and consumers remain untouched; promotion happens after retirement.
  if [[ ${SETUP_S3FS:-false} == true ]]; then
    bash "$assets/install-s3fs.sh" install "$candidate/s3fs"
  else
    bash "$assets/install-s3fs.sh" check
  fi
  [[ ! -f "$candidate/s3fs" ]] || mount_changed=true
  [[ ! -e "$pending" ]] || mount_changed=true
  [[ ! -e "$legacy_unit" ]] || mount_changed=true
  if [[ "$mount_changed" == true ]]; then
    install -d -m 0700 "$(dirname "$config")"
    touch "$pending"
    chmod 0600 "$pending"
  fi
}

retire_consumers_and_storage() {
  if [[ -f "$units/assistant-app.service" ]]; then
    systemctl stop assistant-app.service
  fi
  # Use the new retirement implementation with the OLD configuration until unmounted.
  # This also removes leftover one-off containers when the app unit was inactive.
  retirement_config="$config"
  [[ -f "$retirement_config" ]] || retirement_config="$candidate/storage.conf"
  ASSISTANT_STORAGE_CONFIG="$retirement_config" "$assets/app-host.sh" stop
  if [[ "$mount_changed" == true ]]; then
    if [[ -f "$units/assistant-daily-logs.service" ]]; then
      systemctl stop assistant-daily-logs.service
    fi
    if [[ -f "$config" && -x "$helpers/daily-logs-host.sh" ]]; then
      "$helpers/daily-logs-host.sh" cleanup
    else
      ASSISTANT_STORAGE_CONFIG="$retirement_config" ASSISTANT_HELPER_DIR="$assets" \
        "$assets/daily-logs-host.sh" cleanup
    fi
    # The old shared self-bind must be removed only after its FUSE child and all
    # consumers have gone. Verify its provenance, including on interrupted reruns.
    (
      source "$retirement_config"
      parent="$DATA_ROOT/s3"
      [[ ! -L "$DATA_ROOT" && ! -L "$parent" ]] || { echo 'Symlink in storage paths' >&2; exit 1; }
      if findmnt -rn --mountpoint "$parent" >/dev/null; then
        [[ $(findmnt -rn --mountpoint "$parent" -o UUID) == "$DATA_VOLUME_UUID" &&
           $(findmnt -rn --mountpoint "$parent" -o FSTYPE) == ext4 &&
           $(findmnt -rn --mountpoint "$parent" -o FSROOT) == /s3 ]] || {
          echo 'Refusing unexpected legacy parent mount' >&2; exit 1;
        }
        ! findmnt -rn --mountpoint "$parent/user-data" >/dev/null || {
          echo 'Child mount remains; refusing parent unmount' >&2; exit 1;
        }
        timeout --kill-after=2 15 umount "$parent"
        ! findmnt -rn --mountpoint "$parent" >/dev/null || {
          echo 'Legacy parent mount remains' >&2; exit 1;
        }
      fi
    )
  fi
}

publish_host_services() {
  if [[ -e "$legacy_unit" ]]; then
    systemctl disable --now assistant-daily-logs-prepare.service
    rm -f "$legacy_unit"
  fi
  if [[ -f "$candidate/s3fs" ]]; then
    install -m 0755 "$candidate/s3fs" /usr/local/bin/s3fs
    bash "$assets/install-s3fs.sh" check
  fi
  # Publish only after old storage has been retired when required. Executable helpers
  # are readable by the probe identity; configuration remains accessible only to root.
  install -d -m 0755 "$helpers" "$units"
  install -d -m 0700 "$(dirname "$config")"
  install -m 0600 "$candidate/storage.conf" "$config"
  for file in daily-logs-host.sh app-host.sh recover-daily-logs.sh daily-logs-probe.py; do
    install -m 0755 "$assets/$file" "$helpers/$file"
  done
  install -m 0644 "$candidate/assistant-daily-logs.service" "$units/assistant-daily-logs.service"
  install -m 0644 "$assets/assistant-app.service" "$units/"
  systemctl daemon-reload
  # Enable future boot ordering without starting the app before deployment migrations.
  systemctl enable assistant-daily-logs.service assistant-app.service
  if [[ "$first_install" == true || "$mount_changed" == true ]]; then
    systemctl start assistant-daily-logs.service
  else
    # Deployment is not an implicit recovery mechanism for a failed, unchanged mount.
    systemctl is-active --quiet assistant-daily-logs.service || {
      echo 'Daily-log storage is inactive; run recover-daily-logs.sh before deploying' >&2
      exit 1
    }
  fi
  # Even an unchanged active process must pass I/O readiness; activation alone is insufficient.
  "$helpers/daily-logs-host.sh" ready

  # Clear only once the replacement is fully installed and ready.
  rm -f "$pending"
}

deploy_application() {
  install -m 0644 "$assets/compose.prod.yaml" "$work/compose.yaml"
  install -m 0644 "$assets/Caddyfile" "$work/Caddyfile"
  # The app container runs as uid 10001 with no docker group, so it needs the host group's
  # numeric id to read /var/run/docker.sock. The id differs between AMIs, so resolve it here
  # rather than hardcoding one in the compose file.
  DOCKER_GID=$(getent group docker | cut -d: -f3) || DOCKER_GID=""
  [ -n "$DOCKER_GID" ] || { echo "No docker group on the host; the app cannot reach the Docker socket" >&2; exit 1; }
  export AWS_REGION IMAGE_URI SANDBOX_IMAGE_URI DOMAIN DOCKER_GID
  export ASSISTANT_WORK_DIR="$work"
  python3 - <<'PY'
import json, os, subprocess
prefix = '/assistant-agent/'
keys = {'GOOGLE_CLIENT_ID':'google-client-id', 'GOOGLE_CLIENT_SECRET':'google-client-secret', 'CREDENTIAL_ENCRYPTION_KEY':'credential-encryption-key', 'POSTGRES_PASSWORD':'postgres-password', 'ANTHROPIC_API_KEY':'anthropic-api-key'}
values = {}
for key, suffix in keys.items():
    result = subprocess.run(['aws','ssm','get-parameter','--with-decryption','--region',os.environ['AWS_REGION'],'--name',prefix+suffix,'--query','Parameter.Value','--output','text'],capture_output=True,text=True,check=True)
    values[key] = result.stdout.rstrip('\n')
values['IMAGE_URI'] = os.environ['IMAGE_URI']
values['SANDBOX_IMAGE_URI'] = os.environ['SANDBOX_IMAGE_URI']
values['DOCKER_GID'] = os.environ['DOCKER_GID']
values['DOMAIN'] = os.environ['DOMAIN']
values['SANDBOX_DEPLOYMENT_ID'] = os.environ['SANDBOX_DEPLOYMENT_ID']
environment_path = os.path.join(os.environ['ASSISTANT_WORK_DIR'], '.env')
with open(environment_path,'w') as file:
    for key, value in values.items():
        file.write(key+'='+json.dumps(value)+'\n')
os.chmod(environment_path,0o600)
PY
  cd "$work"
  # PGDATA is a subdirectory, so PostgreSQL also needs access to its parent bind mount.
  # Resolve the postgres UID/GID inside the image rather than assuming host IDs.
  docker compose --env-file .env run --rm --no-deps --user root --entrypoint sh db \
    -c 'chown postgres:postgres /var/lib/postgresql/data && chmod 700 /var/lib/postgresql/data'
  docker compose --env-file .env up -d --wait --wait-timeout 180 db
  docker compose --env-file .env run --rm --no-deps app alembic upgrade head
  systemctl reset-failed assistant-app.service
  systemctl start assistant-app.service
  # The app is exclusively supervised by systemd; Caddy keeps its own Compose policy.
  docker compose --env-file .env up -d --no-deps --remove-orphans caddy
  # Allow the app to initialize before the first HTTPS readiness probe.
  sleep 5
  for attempt in $(seq 1 20); do
    if curl -fsS "https://$DOMAIN/healthz" >/dev/null; then
      # Reclaim the untagged images left behind by this deploy. Dangling only: this never
      # removes a tagged image or one a container references.
      docker image prune -f >/dev/null
      return 0
    fi
    sleep 5
  done
  echo "HTTPS readiness check failed" >&2
  return 1
}

cleanup_deployment() {
  [[ -z ${candidate:-} ]] || rm -rf "$candidate"
}

main() {
  : "${AWS_REGION:?}" "${IMAGE_URI:?}" "${SANDBOX_IMAGE_URI:?}" "${DOMAIN:?}" "${DATA_VOLUME_ID:?}" "${APPLICATION_DATA_BUCKET_NAME:?}"
  initialize_deployment "$@"
  candidate=$(mktemp -d)
  trap cleanup_deployment EXIT
  prepare_host
  prepare_candidates
  retire_consumers_and_storage
  publish_host_services
  deploy_application
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
