#!/bin/bash
set -euo pipefail
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

terraform_output() {
  local value
  if ! value=$(terraform -chdir="$repo_root/terraform/main" output -raw "$1"); then
    echo "Could not read Terraform output '$1'. Initialize and apply terraform/main before deploying." >&2
    return 1
  fi
  if [[ -z "$value" || "$value" == "null" ]]; then
    echo "Terraform output '$1' is empty. Apply terraform/main before deploying." >&2
    return 1
  fi
  printf '%s' "$value"
}

DOMAIN=${DOMAIN:-assistant.nevilgeorge.me}
INSTANCE_ID=${INSTANCE_ID:-$(terraform_output instance_id)}
ECR_REPOSITORY_URL=${ECR_REPOSITORY_URL:-$(terraform_output ecr_repository_url)}
SANDBOX_ECR_REPOSITORY_URL=${SANDBOX_ECR_REPOSITORY_URL:-$(terraform_output sandbox_ecr_repository_url)}
DATA_VOLUME_ID=${DATA_VOLUME_ID:-$(terraform_output data_volume_id)}
region=${AWS_REGION:-us-east-1}
registry=${ECR_REPOSITORY_URL%%/*}
tag=$(git rev-parse --short HEAD)-$(date -u +%Y%m%d%H%M%S)
image="$ECR_REPOSITORY_URL:$tag"
sandbox_image="$SANDBOX_ECR_REPOSITORY_URL:$tag"
# Both repositories live on the same registry host, so this one login covers both pushes.
aws ecr get-login-password --region "$region" | docker login --username AWS --password-stdin "$registry"
docker buildx build --platform linux/arm64 --push -t "$image" .
# The sandbox build context is src/assistant_agent/sandbox_kit, not the repo root, so it
# picks up that directory's own .dockerignore. Sharing $tag with the app image makes the
# two halves of a deploy identifiable as one.
docker buildx build --platform linux/arm64 --push -t "$sandbox_image" \
  -f src/assistant_agent/sandbox_kit/Dockerfile src/assistant_agent/sandbox_kit
command_file=$(mktemp)
trap 'rm -f "$command_file"' EXIT
AWS_REGION="$region" IMAGE_URI="$image" SANDBOX_IMAGE_URI="$sandbox_image" DOMAIN="$DOMAIN" DATA_VOLUME_ID="$DATA_VOLUME_ID" python3 - "$command_file" <<'PY'
import base64, json, os, pathlib, shlex, sys
script = pathlib.Path('deploy/remote.sh').read_bytes()
variables = ' '.join(f'{name}={shlex.quote(os.environ[name])}' for name in ('AWS_REGION','IMAGE_URI','SANDBOX_IMAGE_URI','DOMAIN','DATA_VOLUME_ID'))
command = variables + ' bash -c "$(echo ' + base64.b64encode(script).decode() + ' | base64 -d)"'
pathlib.Path(sys.argv[1]).write_text(json.dumps({'commands':[command]}))
PY
command_id=$(aws ssm send-command --region "$region" --instance-ids "$INSTANCE_ID" --document-name AWS-RunShellScript --parameters "file://$command_file" --query Command.CommandId --output text)
if ! aws ssm wait command-executed --region "$region" --command-id "$command_id" --instance-id "$INSTANCE_ID"; then
  aws ssm get-command-invocation --region "$region" --command-id "$command_id" --instance-id "$INSTANCE_ID" --query '{Status:Status,Output:StandardOutputContent,Error:StandardErrorContent}'
  exit 1
fi
aws ssm get-command-invocation --region "$region" --command-id "$command_id" --instance-id "$INSTANCE_ID" --query '{Status:Status,Output:StandardOutputContent,Error:StandardErrorContent}'
