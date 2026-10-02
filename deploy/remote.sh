#!/bin/bash
set -euo pipefail
: "${AWS_REGION:?}" "${IMAGE_URI:?}" "${SANDBOX_IMAGE_URI:?}" "${DOMAIN:?}" "${DATA_VOLUME_ID:?}"
work=/opt/assistant-agent
mkdir -p "$work" /srv/assistant-agent
volume_serial="${DATA_VOLUME_ID//-/}"
for attempt in $(seq 1 60); do
  device=$(lsblk -ndo NAME,SERIAL | awk -v serial="$volume_serial" '$2 == serial {print "/dev/" $1; exit}')
  if [ -n "$device" ]; then break; fi
  sleep 2
done
[ -n "${device:-}" ] || { echo "Data volume is unavailable"; exit 1; }
if ! blkid "$device" >/dev/null 2>&1; then mkfs.ext4 "$device"; fi
if ! mountpoint -q /srv/assistant-agent; then mount "$device" /srv/assistant-agent; fi
uuid=$(blkid -s UUID -o value "$device")
if ! grep -q "UUID=$uuid " /etc/fstab; then echo "UUID=$uuid /srv/assistant-agent ext4 defaults,nofail 0 2" >> /etc/fstab; fi
mkdir -p /srv/assistant-agent/postgres /srv/assistant-agent/caddy-data /srv/assistant-agent/caddy-config
chmod 700 /srv/assistant-agent/postgres /srv/assistant-agent/caddy-data
# The sandbox runs as uid 1000 (the base image's `node` user, renamed to `agent`) and drops
# all capabilities, so it cannot chown its own bind mount. Do it from the host.
mkdir -p /srv/assistant-agent/sandboxes/1
chown -R 1000:1000 /srv/assistant-agent/sandboxes
chmod 700 /srv/assistant-agent/sandboxes/1
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
# Files are bundled with the image so Run Command needs no source checkout.
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "${IMAGE_URI%%/*}"
docker pull "$IMAGE_URI"
# Pull the sandbox image explicitly rather than leaving it to `compose up`. It is roughly
# 1.9 GiB, and a disk-exhaustion failure is far easier to read here than halfway through
# bringing the stack up.
docker pull "$SANDBOX_IMAGE_URI"
docker run --rm "$IMAGE_URI" sh -c 'cat /app/deploy/compose.prod.yaml' > "$work/compose.yaml"
docker run --rm "$IMAGE_URI" sh -c 'cat /app/deploy/Caddyfile' > "$work/Caddyfile"
# The app container runs as uid 10001 with no docker group, so it needs the host group's
# numeric id to read /var/run/docker.sock. The id differs between AMIs, so resolve it here
# rather than hardcoding one in the compose file.
DOCKER_GID=$(getent group docker | cut -d: -f3) || DOCKER_GID=""
[ -n "$DOCKER_GID" ] || { echo "No docker group on the host; the app cannot reach the Docker socket" >&2; exit 1; }
export AWS_REGION IMAGE_URI SANDBOX_IMAGE_URI DOMAIN DOCKER_GID
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
with open('/opt/assistant-agent/.env','w') as file:
    for key, value in values.items():
        file.write(key+'='+json.dumps(value)+'\n')
os.chmod('/opt/assistant-agent/.env',0o600)
PY
cd "$work"
# PGDATA is a subdirectory, so PostgreSQL also needs access to its parent bind mount.
# Resolve the postgres UID/GID inside the image rather than assuming host IDs.
docker compose --env-file .env run --rm --no-deps --user root --entrypoint sh db \
  -c 'chown postgres:postgres /var/lib/postgresql/data && chmod 700 /var/lib/postgresql/data'
docker compose --env-file .env up -d --wait --wait-timeout 180 db
docker compose --env-file .env run --rm app alembic upgrade head
# The sandbox comes up after the app so that a sandbox failure cannot hold up the HTTPS
# readiness check below, which is what gates the deploy's exit status.
docker compose --env-file .env up -d app caddy sandbox-1
for attempt in $(seq 1 30); do
  if curl -fsS "https://$DOMAIN/healthz" >/dev/null; then
    # Reclaim the untagged images left behind by this deploy. Dangling only: this never
    # removes a tagged image or one a container references.
    docker image prune -f >/dev/null
    exit 0
  fi
  sleep 5
done
echo "HTTPS readiness check failed" >&2
exit 1
