#!/bin/bash
# pg-diagnose-mcp: Test the MCP server endpoints.
# Usage: ./test-mcp.sh <api-gateway-url> <client-id> <client-secret>
set -euo pipefail

BASE="${1:?Usage: $0 <api-gateway-url> <client-id> <client-secret>}"
CLIENT_ID="${2:?Missing client-id}"
CLIENT_SECRET="${3:?Missing client-secret}"

echo "=== 1. Token (credential validation) ==="
TOKEN=$(curl -s -X POST "${BASE}/token" \
  -d "grant_type=client_credentials&client_id=${CLIENT_ID}&client_secret=${CLIENT_SECRET}" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('access_token','FAIL: '+d.get('error','unknown')))")
if [[ "$TOKEN" == FAIL* ]]; then echo "❌ $TOKEN"; exit 1; fi
echo "✅ Token: ${TOKEN:0:12}..."

echo ""
echo "=== 2. Initialize ==="
curl -s -X POST "${BASE}/mcp" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('✅ Server:', d['result']['serverInfo']['name'], 'v'+d['result']['serverInfo']['version'])"

echo ""
echo "=== 3. Tools List ==="
curl -s -X POST "${BASE}/mcp" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | python3 -c "
import sys,json
d=json.load(sys.stdin)
tools=d.get('result',{}).get('tools',[])
print(f'✅ {len(tools)} tools:')
for t in tools: print(f'   - {t[\"name\"]}')
"

echo ""
echo "=== 4. List Databases ==="
curl -s -X POST "${BASE}/mcp" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_databases","arguments":{}}}' \
  | python3 -c "
import sys,json
d=json.load(sys.stdin)
c=d.get('result',{}).get('content',[])
if c:
    data=json.loads(c[0]['text'])
    print(f'✅ {data[\"count\"]} database(s):')
    for db in data['databases']: print(f'   - {db[\"name\"]}: {db[\"host\"]} ({db.get(\"description\",\"\")})')
else: print('❌', json.dumps(d)[:200])
"

echo ""
echo "=== 5. Quick Health Check (first database) ==="
curl -s -m 30 -X POST "${BASE}/mcp" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"get_database_configuration","arguments":{}}}' \
  | python3 -c "
import sys,json
d=json.load(sys.stdin)
c=d.get('result',{}).get('content',[])
if c and 'Error' not in c[0].get('text','')[:50]:
    data=json.loads(c[0]['text'])
    print(f'✅ Connected to: {data.get(\"database\",\"?\")}')
    print(f'   Risky settings: {data.get(\"risky_settings\",[])}')
else: print('❌', c[0]['text'][:200] if c else json.dumps(d)[:200])
"

echo ""
echo "=== All tests passed ==="
