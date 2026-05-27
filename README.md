# pg-diagnose-mcp

A PostgreSQL diagnostic MCP server for [AWS DevOps Agent](https://docs.aws.amazon.com/devopsagent/). Provides 16 read-only troubleshooting tools that give the AI agent direct access to PostgreSQL system views for automated incident investigation.

**No generic SQL execution.** Purpose-built diagnostic tools collect structured evidence — the AI agent explains root causes and recommends actions.

> ⚠️ This sample demonstrates the architecture and integration patterns. You should conduct your own security review, harden credentials, and validate the infrastructure template against your compliance requirements before deploying to production workloads.

## Features

- **16 diagnostic tools** — from broad health checks to deep autovacuum and catalog invalidation analysis
- **Multi-database** — manage multiple PostgreSQL instances (RDS, Aurora, EC2) from one MCP server
- **Tag-based discovery** — add/remove databases by tagging Secrets Manager secrets, no redeployment needed
- **PostgreSQL 14-17 compatible** — auto-detects PG version and uses correct system views
- **Security hardened** — dedicated read-only DB user, Secrets Manager credentials, OAuth validation, VPC isolation
- **Pure Python** — uses pg8000 (BSD license), no C extensions or system dependencies
- **Infrastructure as Code** — CloudFormation template deploys everything

## Architecture

```
DevOps Agent → API Gateway (OAuth) → Lambda (proxy) → AgentCore (VPC) → PostgreSQL
                                                            ↕
                                                    Secrets Manager
                                                  (database registry)
```

| Component | Purpose |
|-----------|---------|
| **API Gateway** | HTTPS endpoint with OAuth 2.0 discovery |
| **Lambda** | OAuth credential validation + AgentCore forwarding |
| **AgentCore** | Managed container runtime in VPC mode |
| **MCP Server** | Python container with 16 diagnostic tools |
| **Secrets Manager** | Database credentials, discovered by tag |
| **VPC Endpoints** | S3, ECR, CloudWatch Logs, STS, Secrets Manager |

## Quick Start

### Prerequisites

- AWS CLI v2.34+
- [AgentCore CLI](https://pypi.org/project/bedrock-agentcore-starter-toolkit/) (`pip install bedrock-agentcore-starter-toolkit`)
- An AWS DevOps Agent space
- One or more PostgreSQL instances (RDS, Aurora, or EC2)

### 1. Deploy Infrastructure

```bash
aws cloudformation deploy \
  --template-file infrastructure/template.yaml \
  --stack-name pg-diagnose-mcp \
  --parameter-overrides \
    VpcId=vpc-xxx \
    SubnetIds=subnet-aaa,subnet-bbb,subnet-ccc \
    RdsSecurityGroupId=sg-xxx \
    OAuthClientId=devops-agent-client \
    OAuthClientSecret=YOUR_SECRET \
  --capabilities CAPABILITY_NAMED_IAM
```

> **Important**: Subnets must be in AgentCore-supported AZs. In us-east-1: `use1-az1` (1b), `use1-az2` (1c), `use1-az4` (1d).

### 2. Deploy AgentCore MCP Server

```bash
cd mcp-server
agentcore configure --create --name pg_diagnose_mcp --entrypoint server.py --protocol MCP --requirements-file requirements.txt --region us-east-1 --non-interactive

# Edit .bedrock_agentcore.yaml:
#   ecr_repository: <from CloudFormation output EcrRepositoryUri>
#   network_mode: VPC
#   security_groups: [<from CloudFormation output SecurityGroupId>]
#   subnets: [<your supported AZ subnets>]

agentcore deploy --auto-update-on-conflict
```

### 3. Update Lambda with AgentCore ARN

```bash
RUNTIME_ARN=$(grep agent_arn .bedrock_agentcore.yaml | awk '{print $2}')
aws lambda update-function-configuration \
  --function-name pg-diagnose-mcp-proxy \
  --environment "Variables={RUNTIME_ARN=${RUNTIME_ARN},BASE_URL=<ApiGatewayUrl>,OAUTH_CLIENT_ID=devops-agent-client,OAUTH_CLIENT_SECRET=YOUR_SECRET,TOKEN_TTL_SECONDS=3600}"
```

### 4. Deploy Lambda Code

```bash
cd lambda-proxy
zip proxy.zip index.py
aws lambda update-function-code --function-name pg-diagnose-mcp-proxy --zip-file fileb://proxy.zip
```

### 5. Create Database User (on each PostgreSQL instance)

```sql
CREATE USER diag_readonly WITH PASSWORD 'YOUR_PASSWORD';
GRANT CONNECT ON DATABASE postgres TO diag_readonly;
GRANT USAGE ON SCHEMA public TO diag_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO diag_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO diag_readonly;
GRANT pg_read_all_stats TO diag_readonly;
GRANT pg_read_all_settings TO diag_readonly;
```

### 6. Add Databases

```bash
scripts/add-database.sh prod-orders \
  --host prod-orders.xxxxx.rds.amazonaws.com \
  --username diag_readonly \
  --password 'YOUR_PASSWORD' \
  --dbname orders \
  --description "Production orders database"
```

### 7. Test

```bash
scripts/test-mcp.sh https://<api-id>.execute-api.us-east-1.amazonaws.com devops-agent-client YOUR_SECRET
```

### 8. Register with DevOps Agent

```bash
aws devops-agent register-service --name pg-diagnose-mcp --service mcpserver \
  --service-details '{"mcpserver":{"name":"pg-diagnose-mcp","endpoint":"https://<api-id>.execute-api.us-east-1.amazonaws.com/mcp","description":"PostgreSQL diagnostic MCP server","authorizationConfig":{"oAuthClientCredentials":{"clientName":"devops-agent","clientId":"devops-agent-client","clientSecret":"YOUR_SECRET","exchangeUrl":"https://<api-id>.execute-api.us-east-1.amazonaws.com/token","scopes":["mcp"]}}}}'

aws devops-agent associate-service --agent-space-id <SPACE_ID> --service-id <SERVICE_ID> \
  --configuration '{"mcpserver":{"tools":["list_databases","diagnose_database_performance","analyze_specific_query","get_query_plan_safe","get_top_query_workload","get_active_sessions_and_locks","get_wait_event_analysis","get_table_health","get_index_health","get_vacuum_and_stats_health","get_database_configuration","get_system_health","get_connection_breakdown","get_autovacuum_workers_status","generate_diagnosis_report","get_catalog_invalidation_risk"]}}'
```

## Managing Databases

```bash
# Add a database
scripts/add-database.sh analytics --host analytics.xxxxx.rds.amazonaws.com --username diag_readonly --password '...'

# Remove from MCP (keep secret)
scripts/remove-database.sh analytics

# Remove permanently
scripts/remove-database.sh analytics --delete-secret
```

Databases are discovered by Secrets Manager tag `mcp-server=pg-diagnose`. The MCP server refreshes every 5 minutes automatically.

## Tool Reference

| Tool | Description |
|------|-------------|
| `list_databases` | List all registered PostgreSQL databases |
| `diagnose_database_performance` | Broad health check: queries, locks, waits, autovacuum, bloat, txid wraparound |
| `analyze_specific_query` | Deep diagnosis for one query: plan, index usage, stale stats |
| `get_query_plan_safe` | Safe EXPLAIN plan (rejects writes, EXPLAIN only by default) |
| `get_top_query_workload` | Most expensive queries from pg_stat_statements |
| `get_active_sessions_and_locks` | Live blocking, locks, long-running sessions, connection breakdown |
| `get_wait_event_analysis` | What sessions are waiting on: IO, Lock, LWLock, Client |
| `get_table_health` | Table size, dead tuples, storage params, autovacuum threshold |
| `get_index_health` | Unused/oversized indexes, FK without indexes |
| `get_vacuum_and_stats_health` | Autovacuum: table overrides, threshold calc, worker status |
| `get_database_configuration` | PostgreSQL config review: memory, connections, autovacuum |
| `get_system_health` | Buffer cache, checkpoints, temp files, txid wraparound, replication |
| `get_connection_breakdown` | Connections by state/user/app, utilization percentage |
| `get_autovacuum_workers_status` | Active workers, progress, all slots busy? |
| `generate_diagnosis_report` | Evidence-based summary combining multiple checks |
| `get_catalog_invalidation_risk` | Detect RELCACHE invalidation storms from frequent DDL (ALTER TABLE) causing replica CPU spikes |

All tools accept an optional `database` parameter. If omitted and only one database is registered, it's used automatically.

## Security

| Layer | Protection |
|-------|-----------|
| OAuth | Client credentials validated at token endpoint |
| Database user | `diag_readonly` — SELECT only + pg_read_all_stats |
| Secrets Manager | Credentials fetched at runtime, never in code |
| Connection | `readonly=True` + `statement_timeout=30s` |
| Network | AgentCore in VPC, RDS private, VPC endpoints only |
| Tools | No generic SQL — only purpose-built diagnostic queries |

## Data Exposure

This tool surfaces database metadata and operational telemetry to the AI agent for troubleshooting. During execution, the following information may be returned in diagnostic output:

| Category | Examples |
|----------|----------|
| Schema metadata | Table names, index names, schema names, constraint definitions |
| Query text | Full SQL from `pg_stat_statements` and `pg_stat_activity` (active/slow queries) |
| Execution plans | EXPLAIN output including table/column references and row estimates |
| User information | Database usernames, application names, client IP addresses |
| Configuration | PostgreSQL parameter values (`pg_settings`) |
| Sizing | Table/index sizes, row counts, dead tuple counts |

**This is by design** — the AI agent needs this context to diagnose performance issues accurately. However, if your environment has strict data classification policies, be aware that:

- Query text may contain literal values or business logic
- Table/column names may reveal domain model details
- Application names and usernames are visible in session data

Consider this when registering the tool with shared or multi-tenant AI agent spaces.

## Cost

| Component | Monthly Cost |
|-----------|-------------|
| VPC Endpoints (5 interface) | ~$35 |
| S3 Gateway Endpoint | Free |
| Secrets Manager | ~$0.40/secret |
| AgentCore | Pay per invocation |
| Lambda + API Gateway | ~$1/million requests |
| **Baseline** | **~$36/month** |

## Cleanup

```bash
# Remove DevOps Agent association
aws devops-agent disassociate-service --agent-space-id <SPACE_ID> --association-id <ASSOC_ID>
aws devops-agent deregister-service --service-id <SERVICE_ID>

# Destroy AgentCore
cd mcp-server && agentcore destroy

# Delete CloudFormation stack
aws cloudformation delete-stack --stack-name pg-diagnose-mcp

# Delete database secrets
scripts/remove-database.sh <name> --delete-secret
```

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
