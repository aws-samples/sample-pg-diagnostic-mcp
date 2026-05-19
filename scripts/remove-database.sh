#!/bin/bash
# pg-diagnose-mcp: Remove a PostgreSQL database from the MCP server registry.
# Usage: ./remove-database.sh <name> [--delete-secret] [--region us-east-1]
set -euo pipefail

NAME="${1:?Usage: $0 <name> [--delete-secret] [--region us-east-1]}"
shift

DELETE_SECRET=false REGION="us-east-1"
while [[ $# -gt 0 ]]; do
  case $1 in
    --delete-secret) DELETE_SECRET=true; shift;;
    --region) REGION="$2"; shift 2;;
    *) echo "Unknown option: $1"; exit 1;;
  esac
done

SECRET_NAME="pg-diagnose-mcp/${NAME}"
REGISTRY_SECRET="pg-diagnose-mcp/registry"

# Get the ARN of the secret to remove from registry
SECRET_ARN=$(aws secretsmanager describe-secret --secret-id "${SECRET_NAME}" --region "${REGION}" --query 'ARN' --output text 2>/dev/null || echo "")

if [ -n "$SECRET_ARN" ]; then
  echo "Removing from registry: ${SECRET_ARN}"
  CURRENT=$(aws secretsmanager get-secret-value --secret-id "${REGISTRY_SECRET}" --region "${REGION}" --query 'SecretString' --output text)
  UPDATED=$(echo "${CURRENT}" | python3 -c "
import sys, json
reg = json.load(sys.stdin)
arn = '${SECRET_ARN}'
reg['allowed_arns'] = [a for a in reg.get('allowed_arns', []) if a != arn]
print(json.dumps(reg))
")
  aws secretsmanager put-secret-value --secret-id "${REGISTRY_SECRET}" --secret-string "${UPDATED}" --region "${REGION}" > /dev/null
  echo "✅ Removed from registry."
fi

if [ "$DELETE_SECRET" = true ]; then
  echo "Deleting secret: ${SECRET_NAME}"
  aws secretsmanager delete-secret --secret-id "${SECRET_NAME}" --force-delete-without-recovery --region "${REGION}" 2>&1
  echo "✅ Secret deleted. Database '${NAME}' removed permanently."
else
  echo "✅ Database '${NAME}' deregistered. Secret still exists at ${SECRET_NAME}."
fi
