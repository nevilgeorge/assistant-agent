#!/bin/bash
# Systemd owns app supervision; Compose's app restart policy must remain "no".
set -euo pipefail
# Deployment installs this root-owned configuration; the override supports isolated tests.
source "${ASSISTANT_STORAGE_CONFIG:-/etc/assistant-agent/storage.conf}"
: "${DATA_ROOT:?}" "${APPLICATION_DATA_BUCKET_NAME:?}" "${SANDBOX_DEPLOYMENT_ID:?}"
work=${ASSISTANT_WORK_DIR:-/opt/assistant-agent}
compose=(docker compose --project-directory "$work" --env-file "$work/.env")
fail() { echo "app-host: $*" >&2; exit 1; }
app_containers() {
  # Compose includes one-off readiness/migration containers with --all. They must
  # exit too: any surviving container can retain the old private FUSE bind.
  if [[ -f "$work/compose.yaml" && -f "$work/.env" ]]; then
    "${compose[@]}" ps --all --quiet app
  fi
}
sandbox_containers() {
  # Both labels are mandatory; preserve other applications and deployments.
  docker ps -aq --filter label=assistant-agent.owner=assistant-agent \
    --filter "label=assistant-agent.deployment=$SANDBOX_DEPLOYMENT_ID"
}
verify_retired() {
  local remaining_app remaining_sandboxes
  remaining_app=$(app_containers) || fail 'unable to verify app retirement'
  remaining_sandboxes=$(sandbox_containers) || fail 'unable to verify sandbox retirement'
  [[ -z "$remaining_app" && -z "$remaining_sandboxes" ]] || fail 'storage consumers remain'
}
case "${1:-}" in
  check)
    # Boot must establish database health before the app starts. Probe through Compose
    # so readiness also verifies the app image's bind mount and runtime identity.
    "${compose[@]}" up -d --wait --wait-timeout 180 db
    "${compose[@]}" run --rm --no-deps --user 10001:10001 --entrypoint python3 app \
      /app/deploy/daily-logs-probe.py "$DATA_ROOT/s3/user-data" "$APPLICATION_DATA_BUCKET_NAME:/user-data"
    ;;
  ready)
    # ExecStartPost waits for the real app, not a one-off. Systemd bounds this
    # entire phase to 60 seconds; each Docker call also has its own deadline.
    for attempt in $(seq 1 30); do
      container=$(timeout --kill-after=2 5 "${compose[@]}" ps --status running --quiet app)
      if [[ -n "$container" ]]; then
        [[ "$container" != *$'\n'* ]] || fail 'expected exactly one running app container'
        exec timeout --kill-after=2 30 docker exec --user 10001:10001 "$container" \
          python3 /app/deploy/daily-logs-probe.py "$DATA_ROOT/s3/user-data" "$APPLICATION_DATA_BUCKET_NAME:/user-data"
      fi
      sleep 1
    done
    fail 'app container readiness deadline'
    ;;
  start)
    # Recreate just the app so every start receives the current private FUSE bind.
    # Stay attached so systemd observes Compose's exit status.
    exec "${compose[@]}" up --no-deps --force-recreate --abort-on-container-exit --exit-code-from app app
    ;;
  stop)
    # Continue retirement after individual failures, then fail closed. A stopped
    # container still holds mount references, so removing it is mandatory.
    stop_status=0
    if [[ -f "$work/compose.yaml" && -f "$work/.env" ]]; then
      "${compose[@]}" stop -t 45 app || stop_status=$?
    fi
    containers=$(app_containers) || stop_status=$?
    if [[ -n "$containers" ]]; then
      while IFS= read -r container; do
        docker rm -f "$container" || stop_status=$?
      done <<< "$containers"
    fi
    containers=$(sandbox_containers) || stop_status=$?
    if [[ -n "$containers" ]]; then
      while IFS= read -r container; do
        docker rm -f "$container" || stop_status=$?
      done <<< "$containers"
    fi
    verify_retired
    exit "$stop_status"
    ;;
  retired) verify_retired ;;
  *) fail 'expected check, ready, start, stop, or retired' ;;
esac
