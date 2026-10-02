#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

for executable in docker python3 curl; do
  command -v "$executable" >/dev/null || { echo "Missing required command: $executable" >&2; exit 1; }
done
docker compose version >/dev/null
docker info >/dev/null
[ -f .env ] || { echo "Create .env from .env.example first." >&2; exit 1; }
[ -s deploy/local-db-password.txt ] || { echo "Create deploy/local-db-password.txt with your local database password." >&2; exit 1; }

# Inspect resolved Compose values without logging credentials or sourcing .env as code.
docker compose config --format json | python3 -c '
import json, pathlib, sys
services = json.load(sys.stdin)["services"]
app = services["app"]["environment"]
missing = [key for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "CREDENTIAL_ENCRYPTION_KEY") if not app.get(key)]
if not services["sandbox-1"]["environment"].get("ANTHROPIC_API_KEY"):
    missing.append("ANTHROPIC_API_KEY")
if missing:
    sys.exit("Set these values in .env: " + ", ".join(missing))
password = pathlib.Path("deploy/local-db-password.txt").read_text().rstrip("\r\n")
if not password or app["DATABASE_URL"] != "postgresql+psycopg://assistant_agent:" + password + "@db:5432/assistant_agent":
    sys.exit("POSTGRES_PASSWORD must match deploy/local-db-password.txt.")
'

docker compose build app sandbox-1
# The socket belongs to the Docker host/VM, whose group can differ from this machine.
DOCKER_GID=$(docker compose run --rm --no-deps --user root \
  --volume /var/run/docker.sock:/var/run/docker.sock --entrypoint python app \
  -c 'import os; print(os.stat("/var/run/docker.sock").st_gid)')
[[ "$DOCKER_GID" =~ ^[0-9]+$ ]] || { echo "Could not determine Docker socket group." >&2; exit 1; }
export DOCKER_GID

docker compose up -d --wait --wait-timeout 120 db
docker compose run --rm --no-deps app alembic upgrade head
docker compose up -d --wait --wait-timeout 120 sandbox-1 app
curl --fail --silent --show-error http://localhost:8000/healthz >/dev/null
echo "Local app ready: http://localhost:8000"
echo "Sign in and send a message from the Ask the assistant card."
echo "Diagnostics: docker compose ps; docker compose logs app sandbox-1"
echo "Stop: docker compose down"
