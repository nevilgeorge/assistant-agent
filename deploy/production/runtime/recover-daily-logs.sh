#!/bin/bash
# Explicit operator recovery only: a failure must leave the application stopped.
set -euo pipefail
helpers=${ASSISTANT_HELPER_DIR:-/usr/local/libexec/assistant-agent}
# Retire all consumers before touching FUSE. The explicit helper also handles
# sandboxes left behind when the app unit was already stopped or had failed.
systemctl stop assistant-app.service
"$helpers/app-host.sh" stop
# Stop the foreground mount process, then verify cleanup even if the unit was inactive.
systemctl stop assistant-daily-logs.service
"$helpers/daily-logs-host.sh" cleanup
# Recheck EBS and mountpoint protection before starting storage. set -e aborts here
# on any failure, keeping the app stopped; there is no automatic recovery loop.
systemctl reset-failed assistant-daily-logs.service assistant-app.service
systemctl start assistant-daily-logs.service
"$helpers/daily-logs-host.sh" ready
# App startup performs its own probe through the container bind. Stop a failed start
# explicitly so systemd's app restart policy cannot continue after failed recovery.
if ! systemctl start assistant-app.service; then
  systemctl stop assistant-app.service
  exit 1
fi
