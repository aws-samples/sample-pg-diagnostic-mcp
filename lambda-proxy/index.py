"""pg-diagnose-mcp Lambda proxy: OAuth 2.0 bridge for AWS DevOps Agent → AgentCore.

All configuration via environment variables:
  RUNTIME_ARN          - AgentCore runtime ARN
  BASE_URL             - API Gateway endpoint URL
  OAUTH_CLIENT_ID      - OAuth client ID for DevOps Agent
  OAUTH_SECRET_ARN     - Secrets Manager ARN for OAuth client secret
  TOKEN_TTL_SECONDS    - Token lifetime (default 3600)
"""
import json, os, uuid, urllib.parse, base64, hashlib, hmac, time
import boto3
from botocore.config import Config

_config = Config(read_timeout=85, connect_timeout=10, retries={"max_attempts": 0})
_region = os.environ.get("AWS_REGION", "us-east-1")
_client = boto3.client("bedrock-agentcore", region_name=_region, config=_config)
_sm = boto3.client("secretsmanager", region_name=_region)

RUNTIME_ARN = os.environ["RUNTIME_ARN"]
BASE = os.environ["BASE_URL"]
CLIENT_ID = os.environ["OAUTH_CLIENT_ID"]
OAUTH_SECRET_ARN = os.environ["OAUTH_SECRET_ARN"]
TOKEN_TTL = int(os.environ.get("TOKEN_TTL_SECONDS", "3600"))

# Fetch and cache OAuth client secret from Secrets Manager on cold start
_oauth_secret_cache = {}

def _get_oauth_secret():
    if not _oauth_secret_cache.get("value"):
        val = _sm.get_secret_value(SecretId=OAUTH_SECRET_ARN)["SecretString"]
        _oauth_secret_cache["value"] = val
    return _oauth_secret_cache["value"]

# Cached initialize response — avoids AgentCore cold start during DevOps Agent validation.
INIT_RESPONSE = json.dumps({
    "jsonrpc": "2.0", "id": 1,
    "result": {"protocolVersion": "2025-03-26", "capabilities": {"tools": {"listChanged": False}},
               "serverInfo": {"name": "pg-diagnose-mcp", "version": "1.0.0"}}
})


def _issue_token():
    """Issue HMAC-signed token with expiry."""
    exp = int(time.time()) + TOKEN_TTL
    payload = f"{uuid.uuid4()}:{exp}"
    sig = hmac.new(_get_oauth_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def _verify_token(token):
    """Verify HMAC signature and expiry."""
    parts = token.split(":")
    if len(parts) != 3:
        return False
    payload = f"{parts[0]}:{parts[1]}"
    try:
        exp = int(parts[1])
    except ValueError:
        return False
    if time.time() > exp:
        return False
    expected = hmac.new(_get_oauth_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(parts[2], expected)


def _parse_sse(raw):
    for line in raw.replace("\r\n", "\n").split("\n"):
        if line.startswith("data: "): return line[6:]
    return raw


def _extract_credentials(event, body):
    """Extract client_id/secret from Basic auth, form body, or JSON body."""
    headers = event.get("headers", {})
    # Basic auth
    auth = headers.get("authorization", "")
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            if ":" in decoded: return decoded.split(":", 1)
        except: pass
    # Decode base64 body if needed
    raw = body
    if event.get("isBase64Encoded"):
        try: raw = base64.b64decode(body).decode()
        except: pass
    # JSON
    try:
        d = json.loads(raw)
        if isinstance(d, dict) and "client_id" in d:
            return d.get("client_id", ""), d.get("client_secret", "")
    except: pass
    # Form-encoded
    params = {}
    for pair in raw.split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            params[urllib.parse.unquote_plus(k)] = urllib.parse.unquote_plus(v)
    return params.get("client_id", ""), params.get("client_secret", "")


def handler(event, context):
    headers = event.get("headers", {})
    body = event.get("body", "") or ""
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "")

    # ── OAuth Discovery ────────────────────────────────────────────────────
    if ".well-known" in path:
        if "oauth-protected-resource" in path:
            return _j(200, {"resource": BASE + "/mcp", "authorization_servers": [BASE], "bearer_methods_supported": ["header"]})
        return _j(200, {"issuer": BASE, "authorization_endpoint": BASE + "/authorize", "token_endpoint": BASE + "/token",
                        "registration_endpoint": BASE + "/register", "response_types_supported": ["code"],
                        "grant_types_supported": ["authorization_code", "client_credentials"],
                        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic", "none"],
                        "code_challenge_methods_supported": ["S256"], "scopes_supported": ["openid", "mcp"]})

    # ── Dynamic Client Registration ────────────────────────────────────────
    if path.endswith("/register") and method == "POST":
        reg = json.loads(body) if body else {}
        return _j(201, {"client_id": str(uuid.uuid4()), "client_name": reg.get("client_name", "devops-agent"),
                        "redirect_uris": reg.get("redirect_uris", []), "grant_types": reg.get("grant_types", ["authorization_code"]),
                        "response_types": reg.get("response_types", ["code"]),
                        "token_endpoint_auth_method": reg.get("token_endpoint_auth_method", "none")})

    # ── OAuth Authorize ────────────────────────────────────────────────────
    if path.endswith("/authorize") and method == "GET":
        qs = event.get("rawQueryString", "")
        params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        return {"statusCode": 302, "headers": {"Location": f"{urllib.parse.unquote(params.get('redirect_uri', ''))}?code={uuid.uuid4()}&state={params.get('state', '')}"}, "body": ""}

    # ── Token Endpoint (validates credentials) ─────────────────────────────
    if path.endswith("/token") and method == "POST":
        cid, csecret = _extract_credentials(event, body)
        if cid != CLIENT_ID or csecret != _get_oauth_secret():
            return _j(401, {"error": "invalid_client", "error_description": "Invalid client_id or client_secret"})
        token = _issue_token()
        return _j(200, {"access_token": token, "token_type": "Bearer", "expires_in": TOKEN_TTL, "scope": "mcp"})

    # ── Bearer Token Validation (HMAC-signed with expiry) ────────────────
    auth = headers.get("authorization", "")
    if not auth.startswith("Bearer ") or not _verify_token(auth[7:]):
        return {"statusCode": 401, "headers": {"Content-Type": "application/json", "WWW-Authenticate": 'Bearer realm="mcp"'},
                "body": json.dumps({"error": "invalid_token", "error_description": "Valid Bearer token required"})}

    # ── Fast Path: initialize (cached, avoids cold start) ──────────────────
    if method == "GET":
        return _j(200, json.loads(INIT_RESPONSE))
    if body:
        try:
            req = json.loads(body)
            m = req.get("method", "")
            if m == "initialize":
                resp = json.loads(INIT_RESPONSE); resp["id"] = req.get("id", 1)
                return _j(200, resp)
            if m == "notifications/initialized":
                return _j(200, "")
        except: pass

    # ── Forward to AgentCore ───────────────────────────────────────────────
    payload = body.encode("utf-8") if body else b"{}"
    kwargs = {"agentRuntimeArn": RUNTIME_ARN, "payload": payload,
              "contentType": headers.get("content-type", "application/json"),
              "accept": "application/json, text/event-stream"}
    mcp_session = headers.get("mcp-session-id") or headers.get("x-mcp-session-id")
    if mcp_session:
        kwargs["mcpSessionId"] = mcp_session
    try:
        resp = _client.invoke_agent_runtime(**kwargs)
        raw = ""
        for k, v in resp.items():
            if hasattr(v, "read"): raw = v.read().decode("utf-8"); break
        rh = {"Content-Type": "application/json"}
        if resp.get("mcpSessionId"): rh["mcp-session-id"] = resp["mcpSessionId"]
        return {"statusCode": resp.get("statusCode", 200), "headers": rh, "body": _parse_sse(raw)}
    except Exception as e:
        print(f"[pg-diagnose] AgentCore error: {type(e).__name__}: {e}")
        return _j(502, {"error": str(e)})


def _j(code, body):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body) if isinstance(body, dict) else (body if isinstance(body, str) else "")}
