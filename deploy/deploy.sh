#!/bin/bash
set -euo pipefail
: "${DOMAIN:?Set DOMAIN after pointing its DNS A record to the Terraform elastic_ip output}"
: "${INSTANCE_ID:?Set INSTANCE_ID from Terraform output}"
: "${ECR_REPOSITORY_URL:?Set ECR_REPOSITORY_URL from Terraform output}"
: "${DATA_VOLUME_ID:?Set DATA_VOLUME_ID from Terraform output}"
region=${AWS_REGION:-us-east-1}
registry=${ECR_REPOSITORY_URL%%/*}
tag=$(git rev-parse --short HEAD)-$(date -u +%Y%m%d%H%M%S)
image="$ECR_REPOSITORY_URL:$tag"
aws ecr get-login-password --region "$region" | docker login --username AWS --password-stdin "$registry"
docker buildx build --platform linux/arm64 --push -t "$image" .
command_file=$(mktemp)
trap 'rm -f "$command_file"' EXIT
AWS_REGION="$region" IMAGE_URI="$image" DOMAIN="$DOMAIN" DATA_VOLUME_ID="$DATA_VOLUME_ID" python3 - "$command_file" <<'PY'
import base64, json, os, pathlib, shlex, sys
script = pathlib.Path('deploy/remote.sh').read_bytes()
variables = ' '.join(f'{name}={shlex.quote(os.environ[name])}' for name in ('AWS_REGION','IMAGE_URI','DOMAIN','DATA_VOLUME_ID'))
command = variables + ' bash -c "$(echo ' + base64.b64encode(script).decode() + ' | base64 -d)"'
pathlib.Path(sys.argv[1]).write_text(json.dumps({'commands':[command]}))
PY
command_id=$(aws ssm send-command --region "$region" --instance-ids "$INSTANCE_ID" --document-name AWS-RunShellScript --parameters "file://$command_file" --query Command.CommandId --output text)
if ! aws ssm wait command-executed --region "$region" --command-id "$command_id" --instance-id "$INSTANCE_ID"; then
  aws ssm get-command-invocation --region "$region" --command-id "$command_id" --instance-id "$INSTANCE_ID" --query '{Status:Status,Output:StandardOutputContent,Error:StandardErrorContent}'
  exit 1
fi
aws ssm get-command-invocation --region "$region" --command-id "$command_id" --instance-id "$INSTANCE_ID" --query '{Status:Status,Output:StandardOutputContent,Error:StandardErrorContent}'
