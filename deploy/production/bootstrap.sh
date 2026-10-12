#!/bin/bash
# Self-contained SSM payload: all host deployment logic comes from the pulled image.
set -euo pipefail
: "${AWS_REGION:?}" "${IMAGE_URI:?}" "${SANDBOX_IMAGE_URI:?}" "${DOMAIN:?}" "${DATA_VOLUME_ID:?}" "${APPLICATION_DATA_BUCKET_NAME:?}"
export AWS_REGION IMAGE_URI SANDBOX_IMAGE_URI DOMAIN DATA_VOLUME_ID APPLICATION_DATA_BUCKET_NAME
export SETUP_S3FS=${SETUP_S3FS:-false}
export SANDBOX_DEPLOYMENT_ID=${SANDBOX_DEPLOYMENT_ID:-production}
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "${IMAGE_URI%%/*}"
docker pull "$IMAGE_URI"
# Fetch both images before any host changes or application downtime.
docker pull "$SANDBOX_IMAGE_URI"
assets=$(mktemp -d)
trap 'rm -rf "$assets"' EXIT
docker run --rm "$IMAGE_URI" tar -C /app/deploy -cf - . | tar -xf - -C "$assets"
bash "$assets/apply.sh" "$assets"
