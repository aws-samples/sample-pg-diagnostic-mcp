#!/bin/bash
# pg-diagnose-mcp: Add a PostgreSQL database to the MCP server registry.
# Usage: ./add-database.sh <name> --host <host> --username <user> --password <pass> [--port 5432] [--dbname postgres] [--description "..."] [--region us-east-1]
set -euo pipefail

NAME="${1:?Usage: $0 <name> --host <host> --username <user> --password <pass>}"
shift

HOST="" USERNAME="" PASSWORD="" PORT="5432" DBNAME="postgres" DESC="" REGION="us-east-1"
while [[ $# -gt 0 ]]; do
  case $1 in
    --host) HOST="$2"; shift 2;;
    --username) USERNAME="$2"; shift 2;;
    --password) PASSWORD="$2"; shift 2;;
    --port) PORT="$2"; shift 2;;
    --dbname) DBNAME="$2"; shift 2;;
    --description) DESC="$2"; shift 2;;
    --region) REGION="$2"; shift 2;;
    *) echo "Unknown option: $1"; exit 1;;
  esac
done

[[ -z "$HOST" ]] && echo "Error: --host required" && exit 1
[[ -z "$USERNAME" ]] && echo "Error: --username required" && exit 1
[[ -z "$PASSWORD" ]] && echo "Error: --password required" && exit 1

SECRET_NAME="pg-diagnose-mcp/${NAME}"
REGISTRY_SECRET="pg-diagnose-mcp/registry"
SECRET_VALUE=$(cat <<EOF
{"host":"${HOST}","port":"${PORT}","username":"${USERNAME}","password":"${PASSWORD}","dbname":"${DBNAME}","description":"${DESC}"}
EOF
)

echo "Creating secret: ${SECRET_NAME}"
SECRET_ARN=$(aws secretsmanager create-secret \
  --name "${SECRET_NAME}" \
  --secret-string "${SECRET_VALUE}" \
  --tags "[{\"Key\":\"auto-delete\",\"Value\":\"no\"}]" \
  --region "${REGION}" \
  --query 'ARN' --output text)

echo "Registering ARN in registry: ${REGISTRY_SECRET}"
CURRENT=$(aws secretsmanager get-secret-value --secret-id "${REGISTRY_SECRET}" --region "${REGION}" --query 'SecretString' --output text)
UPDATED=$(echo "${CURRENT}" | python3 -c "
import sys, json
reg = json.load(sys.stdin)
arn = '${SECRET_ARN}'
if arn not in reg.get('allowed_arns', []):
    reg.setdefault('allowed_arns', []).append(arn)
print(json.dumps(reg))
")
aws secretsmanager put-secret-value --secret-id "${REGISTRY_SECRET}" --secret-string "${UPDATED}" --region "${REGION}" > /dev/null

echo ""
echo "✅ Database '${NAME}' added to pg-diagnose-mcp"
echo "   ARN: ${SECRET_ARN}"
echo "   The MCP server will discover it within 5 minutes (or on next container start)."
