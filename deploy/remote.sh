#!/bin/bash
set -euo pipefail
: "${AWS_REGION:?}" "${IMAGE_URI:?}" "${DOMAIN:?}" "${DATA_VOLUME_ID:?}"
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
# Files are bundled with the image so Run Command needs no source checkout.
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "${IMAGE_URI%%/*}"
docker pull "$IMAGE_URI"
docker run --rm "$IMAGE_URI" sh -c 'cat /app/deploy/compose.prod.yaml' > "$work/compose.yaml"
docker run --rm "$IMAGE_URI" sh -c 'cat /app/deploy/Caddyfile' > "$work/Caddyfile"
export AWS_REGION IMAGE_URI DOMAIN
python3 - <<'PY'
import json, os, subprocess
prefix = '/assistant-agent/'
keys = {'GOOGLE_CLIENT_ID':'google-client-id', 'GOOGLE_CLIENT_SECRET':'google-client-secret', 'CREDENTIAL_ENCRYPTION_KEY':'credential-encryption-key', 'POSTGRES_PASSWORD':'postgres-password'}
values = {}
for key, suffix in keys.items():
    result = subprocess.run(['aws','ssm','get-parameter','--with-decryption','--region',os.environ['AWS_REGION'],'--name',prefix+suffix,'--query','Parameter.Value','--output','text'],capture_output=True,text=True,check=True)
    values[key] = result.stdout.rstrip('\n')
values['IMAGE_URI'] = os.environ['IMAGE_URI']
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
docker compose --env-file .env up -d app caddy
for attempt in $(seq 1 30); do
  if curl -fsS "https://$DOMAIN/healthz" >/dev/null; then exit 0; fi
  sleep 5
done
echo "HTTPS readiness check failed" >&2
exit 1
