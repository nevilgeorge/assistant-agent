#!/bin/bash
set -euo pipefail
dnf install -y docker amazon-ssm-agent awscli-2 util-linux curl
systemctl enable --now docker amazon-ssm-agent
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL https://github.com/docker/compose/releases/download/v2.39.4/docker-compose-linux-aarch64 -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
mkdir -p /srv/assistant-agent
# EBS attachment appears after boot. Find its Nitro device by the volume's filesystem label.
# The deploy command performs the mount after attachment is complete.
