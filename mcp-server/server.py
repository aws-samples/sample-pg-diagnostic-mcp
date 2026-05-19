"""pg-diagnose-mcp: PostgreSQL Diagnostic MCP Server for AWS DevOps Agent.

Multi-database, read-only diagnostic tools. No generic run_sql().
Databases loaded from explicit ARN allowlist in registry secret (pg-diagnose-mcp/registry).
Uses pg8000 (BSD license) as the PostgreSQL driver.
"""
import json, os, re, traceback, threading, time
from http.server import HTTPServer, BaseHTTPRequestHandler
import pg8000.native
import boto3

# ── Configuration ───────────────────────────────────────────────────────────
REGISTRY_SECRET = os.environ.get("REGISTRY_SECRET", "pg-diagnose-mcp/registry")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
REFRESH_INTERVAL = int(os.environ.get("DB_REFRESH_INTERVAL", "300"))

# ── Database Registry ───────────────────────────────────────────────────────
_databases = {}
_db_lock = threading.Lock()
_sm = boto3.client("secretsmanager", region_name=AWS_REGION)


def _discover_databases():
    """Load databases from explicit ARN allowlist in registry secret."""
    found = {}
    try:
        registry = json.loads(_sm.get_secret_value(SecretId=REGISTRY_SECRET)["SecretString"])
        allowed_arns = registry.get("allowed_arns", [])
        for arn in allowed_arns:
            try:
                resp = _sm.get_secret_value(SecretId=arn)
                val = json.loads(resp["SecretString"])
                name = resp["Name"].rsplit("/", 1)[-1]
                found[name] = {
                    "host": val["host"], "port": int(val.get("port", 5432)),
                    "user": val["username"], "password": val["password"],
                    "database": val.get("dbname", "postgres"),
                    "description": val.get("description", ""),
                }
            except Exception as e:
                print(f"[pg-diagnose] Failed to load secret {arn}: {e}")
    except Exception as e:
        print(f"[pg-diagnose] Registry load error: {e}")
    with _db_lock:
        _databases.clear()
        _databases.update(found)
    print(f"[pg-diagnose] Loaded {len(found)} database(s): {', '.join(found.keys()) or 'none'}")


def _refresh_loop():
    while True:
        time.sleep(REFRESH_INTERVAL)
        _discover_databases()


def _get_db(name):
    with _db_lock:
        return _databases.get(name)


def _list_db_names():
    with _db_lock:
        return list(_databases.keys())


def _conn(db_name):
    db = _get_db(db_name)
    if not db:
        raise ValueError(f"Database '{db_name}' not found. Available: {', '.join(_list_db_names())}")
    c = pg8000.native.Connection(
        host=db["host"], port=db["port"], user=db["user"],
        password=db["password"], database=db["database"], timeout=10
    )
    c.run("SET statement_timeout = '30s'")
    c.run("SET default_transaction_read_only = on")
    return c


def _q(db_name, sql, params=None):
    """Execute query and return list of dicts."""
    c = _conn(db_name)
    try:
        if params:
            rows = c.run(sql, **{}) if not params else c.run(sql, *params) if isinstance(params, (list, tuple)) else c.run(sql, *[params])
        else:
            rows = c.run(sql)
        if rows and c.columns:
            cols = [col["name"] for col in c.columns]
            return [dict(zip(cols, row)) for row in rows]
        return []
    finally:
        c.close()


def _j(obj):
    return json.dumps(obj, default=str)


def _safe_select(sql):
    return sql.strip().split()[0].lower() in ("select", "with", "explain", "show") if sql.strip() else False


def _resolve_db(args):
    name = args.get("database")
    if name:
        if not _get_db(name):
            _discover_databases()
        return name
    names = _list_db_names()
    if not names:
        _discover_databases()
        names = _list_db_names()
    if len(names) == 1:
        return names[0]
    if not names:
        raise ValueError("No databases registered. Add one with: scripts/add-database.sh")
    raise ValueError(f"Multiple databases available ({', '.join(names)}). Specify 'database' parameter.")


_IDENT_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')

def _safe_ident(name):
    """Validate SQL identifier (schema/table name) against allowlist pattern."""
    if not name or not _IDENT_RE.match(name):
        raise ValueError(f"Invalid identifier: {name!r}")
    return name


# ── Tool Definitions ────────────────────────────────────────────────────────
_DB_PARAM = {"database": {"type": "string", "description": "Database name (from list_databases). Optional if only one database is registered."}}

TOOLS = [
    {"name": "list_databases", "description": "List all registered PostgreSQL databases available for diagnostics.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "diagnose_database_performance", "description": "Broad health check: slow queries, locks, waits, autovacuum, bloat, connections, txid wraparound, WAL pressure, long-running queries.", "inputSchema": {"type": "object", "properties": {**_DB_PARAM}}},
    {"name": "analyze_specific_query", "description": "Deep diagnosis for one query: plan, index usage, seq scans, stale stats, bloat.", "inputSchema": {"type": "object", "properties": {**_DB_PARAM, "query_text": {"type": "string"}, "include_plan": {"type": "boolean", "description": "Include EXPLAIN (default true)"}}, "required": ["query_text"]}},
    {"name": "get_query_plan_safe", "description": "Safe EXPLAIN plan for a SELECT query. Rejects writes.", "inputSchema": {"type": "object", "properties": {**_DB_PARAM, "query_text": {"type": "string"}, "mode": {"type": "string", "description": "explain_only (default) or explain_analyze"}}, "required": ["query_text"]}},
    {"name": "get_top_query_workload", "description": "Most expensive queries from pg_stat_statements.", "inputSchema": {"type": "object", "properties": {**_DB_PARAM, "limit": {"type": "integer"}, "sort_by": {"type": "string", "description": "total_exec_time|mean_exec_time|calls|shared_blks_read|temp_blks_written"}}}},
    {"name": "get_active_sessions_and_locks", "description": "Live blocking, locks, long-running sessions, connection breakdown.", "inputSchema": {"type": "object", "properties": {**_DB_PARAM, "include_idle_in_transaction": {"type": "boolean"}}}},
    {"name": "get_wait_event_analysis", "description": "What sessions are waiting on: IO, Lock, LWLock, Client, IPC.", "inputSchema": {"type": "object", "properties": {**_DB_PARAM}}},
    {"name": "get_table_health", "description": "Table health: size, dead tuples, bloat estimate, storage params, autovacuum threshold calculation.", "inputSchema": {"type": "object", "properties": {**_DB_PARAM, "schema": {"type": "string"}, "table": {"type": "string"}}, "required": ["table"]}},
    {"name": "get_index_health", "description": "Index health: unused, oversized, FK without indexes.", "inputSchema": {"type": "object", "properties": {**_DB_PARAM, "schema": {"type": "string"}, "table": {"type": "string"}}, "required": ["table"]}},
    {"name": "get_vacuum_and_stats_health", "description": "Autovacuum diagnosis: table overrides, threshold calc, worker status.", "inputSchema": {"type": "object", "properties": {**_DB_PARAM, "schema": {"type": "string"}, "table": {"type": "string"}}, "required": ["table"]}},
    {"name": "get_database_configuration", "description": "PostgreSQL config review: memory, connections, autovacuum, WAL, checkpoints.", "inputSchema": {"type": "object", "properties": {**_DB_PARAM}}},
    {"name": "get_system_health", "description": "System health: buffer cache, checkpoint stats, temp files, txid wraparound (database + per-table), replication lag and slots, database size breakdown, stats freshness.", "inputSchema": {"type": "object", "properties": {**_DB_PARAM}}},
    {"name": "get_connection_breakdown", "description": "Connection analysis: by state, user, app, client. Identifies pool pressure.", "inputSchema": {"type": "object", "properties": {**_DB_PARAM}}},
    {"name": "get_autovacuum_workers_status", "description": "Autovacuum worker activity: which tables, progress, all slots busy?", "inputSchema": {"type": "object", "properties": {**_DB_PARAM}}},
    {"name": "generate_diagnosis_report", "description": "Evidence-based summary combining multiple diagnostic checks.", "inputSchema": {"type": "object", "properties": {**_DB_PARAM, "incident_context": {"type": "string", "description": "Problem description"}}, "required": ["incident_context"]}},
]

INIT_RESULT = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "pg-diagnose-mcp", "version": "1.0.0"}}


# ── Tool Implementations (1-7) ─────────────────────────────────────────────
def list_databases(args):
    with _db_lock:
        dbs = [{"name": k, "host": v["host"], "port": v["port"], "database": v["database"], "description": v.get("description", "")} for k, v in _databases.items()]
    return {"databases": dbs, "count": len(dbs)}


def diagnose_database_performance(args):
    db = _resolve_db(args)
    q = lambda sql, p=None: _q(db, sql, p)
    findings = []
    try:
        findings.append({"check": "slow_queries", "data": q("SELECT query, calls, mean_exec_time, total_exec_time, shared_blks_read, temp_blks_written FROM pg_stat_statements ORDER BY mean_exec_time DESC LIMIT 5")})
    except:
        findings.append({"check": "slow_queries", "error": "pg_stat_statements not available"})
    findings.append({"check": "sessions", "data": q("SELECT count(*) as total, count(*) FILTER (WHERE state='active') as active, count(*) FILTER (WHERE state='idle in transaction') as idle_in_tx, count(*) FILTER (WHERE wait_event_type IS NOT NULL AND state='active') as waiting FROM pg_stat_activity WHERE backend_type='client backend'")})
    findings.append({"check": "blocking", "data": q("SELECT count(*) as blocked_count FROM pg_stat_activity WHERE wait_event_type='Lock' AND state='active'")})
    findings.append({"check": "autovacuum_health", "data": q("SELECT relname, n_dead_tup, n_live_tup, CASE WHEN n_live_tup>0 THEN round(n_dead_tup::numeric/n_live_tup,4) ELSE 0 END as dead_ratio, last_autovacuum, last_autoanalyze FROM pg_stat_user_tables WHERE n_dead_tup > 1000 ORDER BY n_dead_tup DESC LIMIT 10")})
    findings.append({"check": "autovacuum_workers", "data": q("SELECT pid, query, age(clock_timestamp(), xact_start) as duration FROM pg_stat_activity WHERE query LIKE 'autovacuum:%'")})
    max_c = q("SELECT setting::int as max_connections FROM pg_settings WHERE name='max_connections'")
    cur_c = q("SELECT count(*) as current_connections FROM pg_stat_activity")
    findings.append({"check": "connections", "max": max_c, "current": cur_c})
    findings.append({"check": "bloat_risk", "data": q("SELECT schemaname, relname, n_dead_tup, pg_size_pretty(pg_total_relation_size(schemaname||'.'||relname)) as size FROM pg_stat_user_tables WHERE n_dead_tup > 10000 ORDER BY n_dead_tup DESC LIMIT 5")})
    findings.append({"check": "txid_wraparound", "data": q("SELECT datname, age(datfrozenxid) as xid_age, current_setting('autovacuum_freeze_max_age')::bigint as freeze_max, round(100.0*age(datfrozenxid)/current_setting('autovacuum_freeze_max_age')::bigint,1) as pct_towards_wraparound FROM pg_database WHERE datname=current_database()")})
    findings.append({"check": "bgwriter", "data": q("SELECT buffers_clean, maxwritten_clean, buffers_alloc FROM pg_stat_bgwriter")})
    findings.append({"check": "temp_files", "data": q("SELECT datname, temp_files, pg_size_pretty(temp_bytes) as temp_bytes FROM pg_stat_database WHERE datname=current_database()")})
    # Long-running queries (> 30 seconds)
    findings.append({"check": "long_running_queries", "data": q("SELECT pid, usename, application_name, state, age(clock_timestamp(), query_start) as duration, left(query, 200) as query_preview FROM pg_stat_activity WHERE state='active' AND query NOT LIKE 'autovacuum:%' AND age(clock_timestamp(), query_start) > interval '30 seconds' AND backend_type='client backend' AND pid != pg_backend_pid() ORDER BY query_start LIMIT 10")})
    severity = "low"
    for f in findings:
        d = f.get("data", [])
        if f["check"] == "blocking" and d and int(d[0].get("blocked_count", 0)) > 0: severity = "high"
        if f["check"] == "autovacuum_health" and isinstance(d, list) and len(d) > 3 and severity != "high": severity = "medium"
        if f["check"] == "txid_wraparound" and d and float(d[0].get("pct_towards_wraparound", 0)) > 50: severity = "high"
    return {"database": db, "severity": severity, "main_findings": findings, "recommended_next_steps": ["Use get_table_health(table) on flagged tables", "Use get_autovacuum_workers_status() to check worker activity", "Use get_system_health() for WAL/buffer/txid details"]}


def analyze_specific_query(args):
    db = _resolve_db(args)
    q = lambda sql, p=None: _q(db, sql, p)
    sql = args["query_text"]
    evidence, causes = {}, []
    if args.get("include_plan", True) and _safe_select(sql):
        try:
            plan = q(f"EXPLAIN (FORMAT JSON) {sql}")
            evidence["plan"] = plan
            ps = _j(plan)
            if "Seq Scan" in ps: causes.append("Sequential scan detected")
            if "Sort" in ps: causes.append("Sort operation may spill to disk")
            if "Nested Loop" in ps: causes.append("Nested loop join detected")
        except Exception as e:
            evidence["plan_error"] = str(e)
    tables = q("SELECT relname, n_live_tup, n_dead_tup, CASE WHEN n_live_tup>0 THEN round(n_dead_tup::numeric/n_live_tup,4) ELSE 0 END as dead_ratio, last_autovacuum, last_autoanalyze, seq_scan, idx_scan FROM pg_stat_user_tables ORDER BY n_dead_tup DESC LIMIT 20")
    evidence["table_stats"] = tables
    for t in tables:
        if t.get("dead_ratio") and float(t["dead_ratio"]) > 0.1: causes.append(f"High dead tuple ratio on {t['relname']}: {t['dead_ratio']}")
        if t.get("seq_scan") and t.get("idx_scan") and int(t["seq_scan"]) > int(t.get("idx_scan", 0)) * 10: causes.append(f"Heavy sequential scans on {t['relname']}")
    return {"database": db, "likely_root_causes": causes or ["No obvious issues found"], "evidence": evidence}


def get_query_plan_safe(args):
    db = _resolve_db(args)
    sql = args["query_text"].strip().rstrip(";")
    if not _safe_select(sql): return {"error": "Only SELECT/WITH queries allowed"}
    mode = args.get("mode", "explain_only")
    cmd = "EXPLAIN (FORMAT JSON)" if mode != "explain_analyze" else "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)"
    plan = _q(db, f"{cmd} {sql}")
    risks, ps = [], _j(plan)
    if "Seq Scan" in ps: risks.append("Sequential scan on large table")
    if "Sort" in ps: risks.append("Sort operation may spill to disk")
    if "Nested Loop" in ps: risks.append("Nested loop join")
    return {"database": db, "plan": plan, "plan_risks": risks}


def get_top_query_workload(args):
    db = _resolve_db(args)
    ALLOWED_SORT = {"total_exec_time", "mean_exec_time", "calls", "shared_blks_read", "temp_blks_written", "rows"}
    sort = args.get("sort_by", "total_exec_time")
    if sort not in ALLOWED_SORT: sort = "total_exec_time"
    limit = min(args.get("limit", 20), 50)
    try:
        return {"database": db, "queries": _q(db, f"SELECT queryid, query as query_sample, calls, round(mean_exec_time::numeric,2) as mean_exec_time_ms, round(total_exec_time::numeric,2) as total_exec_time_ms, shared_blks_read, shared_blks_hit, temp_blks_written, rows FROM pg_stat_statements ORDER BY {sort} DESC LIMIT $1", [limit])}
    except:
        return {"database": db, "error": "pg_stat_statements not available. Install with: CREATE EXTENSION pg_stat_statements;"}


def get_active_sessions_and_locks(args):
    db = _resolve_db(args)
    q = lambda sql, p=None: _q(db, sql, p)
    incl = args.get("include_idle_in_transaction", True)
    w = "WHERE backend_type='client backend' AND pid != pg_backend_pid()"
    if not incl: w += " AND state != 'idle in transaction'"
    sessions = q(f"SELECT pid, state, wait_event_type, wait_event, query, usename, application_name, client_addr, age(clock_timestamp(), xact_start) as tx_duration FROM pg_stat_activity {w} ORDER BY xact_start NULLS LAST")
    blocked = q("SELECT blocked.pid as blocked_pid, blocked.query as blocked_query, blocker.pid as blocker_pid, blocker.query as blocker_query FROM pg_stat_activity blocked JOIN LATERAL (SELECT unnest(pg_blocking_pids(blocked.pid)) as pid) bp ON true JOIN pg_stat_activity blocker ON blocker.pid = bp.pid WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0")
    by_state = q("SELECT state, count(*) as cnt FROM pg_stat_activity WHERE backend_type='client backend' GROUP BY state ORDER BY cnt DESC")
    by_wait = q("SELECT wait_event_type, wait_event, count(*) as cnt FROM pg_stat_activity WHERE wait_event IS NOT NULL AND backend_type='client backend' GROUP BY wait_event_type, wait_event ORDER BY cnt DESC LIMIT 10")
    return {"database": db, "blocking_detected": len(blocked) > 0, "blockers": blocked, "active_sessions": sessions[:50], "breakdown_by_state": by_state, "breakdown_by_wait": by_wait}


def get_wait_event_analysis(args):
    db = _resolve_db(args)
    waits = _q(db, "SELECT wait_event_type, wait_event, count(*) as session_count FROM pg_stat_activity WHERE wait_event IS NOT NULL AND state='active' GROUP BY wait_event_type, wait_event ORDER BY session_count DESC")
    interp = []
    for w in waits:
        wt, we, n = w.get("wait_event_type", ""), w.get("wait_event", ""), int(w.get("session_count", 0))
        if wt == "IO": interp.append(f"{we}: {n} sessions waiting on disk I/O")
        elif wt == "Lock": interp.append(f"{we}: {n} sessions blocked by locks")
        elif wt == "LWLock":
            if "Buffer" in we: interp.append(f"{we}: {n} sessions — shared buffer contention")
            elif "WAL" in we: interp.append(f"{we}: {n} sessions — WAL write pressure")
            else: interp.append(f"{we}: {n} sessions — internal lock contention")
        elif wt == "Client": interp.append(f"{we}: {n} sessions waiting for client")
    return {"database": db, "top_waits": waits, "interpretation": interp or ["No significant waits detected"]}


# ── Tool Implementations (8-15) ────────────────────────────────────────────
def get_table_health(args):
    db = _resolve_db(args)
    s, t = _safe_ident(args.get("schema", "public")), _safe_ident(args["table"])
    rows = _q(db, "SELECT s.relname, s.n_live_tup, s.n_dead_tup, CASE WHEN s.n_live_tup>0 THEN round(s.n_dead_tup::numeric/s.n_live_tup,4) ELSE 0 END as dead_tuple_ratio, s.seq_scan, s.idx_scan, s.n_tup_ins, s.n_tup_upd, s.n_tup_del, s.n_tup_hot_upd, s.last_vacuum, s.last_autovacuum, s.last_analyze, s.last_autoanalyze, pg_size_pretty(pg_total_relation_size(s.schemaname||'.'||s.relname)) as total_size, c.reloptions as storage_parameters FROM pg_stat_user_tables s JOIN pg_class c ON c.relname=s.relname JOIN pg_namespace n ON n.oid=c.relnamespace AND n.nspname=s.schemaname WHERE s.schemaname=$1 AND s.relname=$2", [s, t])
    if not rows: return {"error": f"Table {s}.{t} not found in {db}"}
    r = rows[0]
    issues = []
    dr = float(r.get("dead_tuple_ratio", 0))
    if dr > 0.2: issues.append("High dead tuples — autovacuum may not be keeping up")
    elif dr > 0.1: issues.append("Moderate dead tuple ratio")
    if r.get("seq_scan") and r.get("idx_scan") and int(r["seq_scan"]) > int(r.get("idx_scan", 0)) * 5:
        issues.append("More sequential scans than index scans")
    if not r.get("last_autoanalyze") and not r.get("last_analyze"):
        issues.append("Statistics may be stale — never analyzed")
    vs = {p["name"]: p["setting"] for p in _q(db, "SELECT name, setting FROM pg_settings WHERE name IN ('autovacuum_vacuum_threshold','autovacuum_vacuum_scale_factor')")}
    threshold = int(vs.get("autovacuum_vacuum_threshold", "50"))
    scale = float(vs.get("autovacuum_vacuum_scale_factor", "0.2"))
    reloptions = r.get("storage_parameters") or []
    if isinstance(reloptions, list):
        for opt in reloptions:
            if "vacuum_scale_factor" in str(opt): scale = float(str(opt).split("=")[1])
    live, dead = int(r.get("n_live_tup", 0)), int(r.get("n_dead_tup", 0))
    computed = threshold + int(scale * live)
    score = "good" if not issues else ("poor" if dr > 0.2 else "fair")
    # Bloat estimate based on dead tuples and fillfactor
    bloat_estimate = {}
    try:
        sizes = _q(db, "SELECT pg_relation_size(quote_ident($1)||'.'||quote_ident($2)) as table_bytes, pg_total_relation_size(quote_ident($1)||'.'||quote_ident($2)) as total_bytes, pg_indexes_size(quote_ident($1)||'.'||quote_ident($2)::regclass) as index_bytes", [s, t])
        if sizes:
            tb = int(sizes[0]["table_bytes"])
            if tb > 0 and live > 0:
                avg_row = tb / (live + dead) if (live + dead) > 0 else 0
                expected = int(avg_row * live)
                bloat_bytes = max(0, tb - expected)
                bloat_estimate = {"table_bytes": tb, "expected_bytes": expected, "bloat_bytes": bloat_bytes, "bloat_pct": round(100.0 * bloat_bytes / tb, 1) if tb > 0 else 0, "total_size": sizes[0]["total_bytes"], "index_size": sizes[0]["index_bytes"]}
                if bloat_estimate["bloat_pct"] > 40: issues.append(f"Estimated table bloat: {bloat_estimate['bloat_pct']}%")
    except:
        pass
    return {"database": db, "table": f"{s}.{t}", "health_score": score, "dead_tuple_ratio": dr, "stats": r, "bloat_estimate": bloat_estimate, "autovacuum_threshold": {"computed": computed, "current_dead": dead, "should_trigger": dead > computed, "table_overrides": reloptions}, "issues": issues}


def get_index_health(args):
    db = _resolve_db(args)
    s, t = _safe_ident(args.get("schema", "public")), _safe_ident(args["table"])
    indexes = _q(db, "SELECT indexrelname, idx_scan, idx_tup_read, pg_size_pretty(pg_relation_size(indexrelid)) as index_size, pg_relation_size(indexrelid) as size_bytes FROM pg_stat_user_indexes WHERE schemaname=$1 AND relname=$2 ORDER BY idx_scan", [s, t])
    unused = [i for i in indexes if int(i.get("idx_scan", 0)) == 0]
    fk_no_idx = _q(db, "SELECT conname, pg_get_constraintdef(c.oid) as definition FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE contype='f' AND n.nspname=$1 AND conrelid=(SELECT oid FROM pg_class WHERE relname=$2 AND relnamespace=n.oid) AND NOT EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid=c.conrelid AND i.indkey::text LIKE c.conkey[1]::text||'%')", [s, t])
    tbl = _q(db, "SELECT pg_relation_size(quote_ident($1)||'.'||quote_ident($2)) as s", [s, t])
    tb = int(tbl[0]["s"]) if tbl else 0
    oversized = [i for i in indexes if int(i.get("size_bytes", 0)) > tb and tb > 0]
    recs = []
    if unused: recs.append(f"{len(unused)} unused index(es)")
    if fk_no_idx: recs.append(f"{len(fk_no_idx)} FK(s) without indexes")
    if oversized: recs.append(f"{len(oversized)} index(es) larger than table (possible bloat)")
    return {"database": db, "indexes": indexes, "unused_indexes": unused, "possible_missing_fk_indexes": fk_no_idx, "oversized_indexes": oversized, "recommendations": recs}


def get_vacuum_and_stats_health(args):
    db = _resolve_db(args)
    q = lambda sql, p=None: _q(db, sql, p)
    s, t = _safe_ident(args.get("schema", "public")), _safe_ident(args["table"])
    rows = q("SELECT relname, n_dead_tup, n_live_tup, n_tup_ins, n_tup_upd, n_tup_del, CASE WHEN n_live_tup>0 THEN round(n_dead_tup::numeric/n_live_tup,4) ELSE 0 END as dead_ratio, last_vacuum, last_autovacuum, last_analyze, last_autoanalyze FROM pg_stat_user_tables WHERE schemaname=$1 AND relname=$2", [s, t])
    if not rows: return {"error": f"Table {s}.{t} not found in {db}"}
    r = rows[0]
    settings = q("SELECT name, setting FROM pg_settings WHERE name IN ('autovacuum','autovacuum_vacuum_scale_factor','autovacuum_analyze_scale_factor','autovacuum_vacuum_threshold','autovacuum_analyze_threshold','autovacuum_naptime','autovacuum_max_workers','autovacuum_vacuum_cost_delay','autovacuum_vacuum_cost_limit')")
    sd = {p["name"]: p["setting"] for p in settings}
    reloptions = q("SELECT reloptions FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relname=$1 AND n.nspname=$2", [t, s])
    overrides = reloptions[0].get("reloptions") if reloptions and reloptions[0].get("reloptions") else []
    threshold, scale_f = int(sd.get("autovacuum_vacuum_threshold", "50")), float(sd.get("autovacuum_vacuum_scale_factor", "0.2"))
    if isinstance(overrides, list):
        for opt in overrides:
            if "vacuum_scale_factor" in str(opt): scale_f = float(str(opt).split("=")[1])
    live, dead = int(r.get("n_live_tup", 0)), int(r.get("n_dead_tup", 0))
    computed = threshold + int(scale_f * live)
    workers = q("SELECT pid, query, age(clock_timestamp(), xact_start) as duration FROM pg_stat_activity WHERE query LIKE $1", [f"autovacuum:%{t}%"])
    issues = []
    dr = float(r.get("dead_ratio", 0))
    if dr > 0.2: issues.append("Autovacuum may not be running often enough")
    if dead > computed and not workers: issues.append(f"Dead tuples ({dead}) exceed threshold ({computed}) but no worker running")
    if workers: issues.append(f"Autovacuum worker running (pid: {workers[0]['pid']})")
    return {"database": db, "autovacuum_risk": "high" if dr > 0.2 else ("medium" if dr > 0.1 else "low"), "table_stats": r, "autovacuum_settings": sd, "table_overrides": overrides, "threshold": {"computed": computed, "dead": dead, "should_trigger": dead > computed}, "workers": workers, "issues": issues}


def get_database_configuration(args):
    db = _resolve_db(args)
    params = _q(db, "SELECT name, setting, unit, short_desc FROM pg_settings WHERE name IN ('max_connections','work_mem','maintenance_work_mem','shared_buffers','effective_cache_size','statement_timeout','idle_in_transaction_session_timeout','autovacuum','autovacuum_vacuum_scale_factor','autovacuum_analyze_scale_factor','autovacuum_vacuum_threshold','autovacuum_naptime','autovacuum_max_workers','autovacuum_vacuum_cost_delay','autovacuum_vacuum_cost_limit','random_page_cost','effective_io_concurrency','wal_level','max_wal_size','checkpoint_completion_target','checkpoint_timeout')")
    d = {p["name"]: p["setting"] for p in params}
    risky = []
    if d.get("autovacuum") == "off": risky.append("autovacuum is OFF")
    if d.get("statement_timeout") == "0": risky.append("No statement_timeout")
    if d.get("idle_in_transaction_session_timeout") == "0": risky.append("No idle_in_transaction_session_timeout")
    if int(d.get("max_connections", "100")) > 500: risky.append(f"max_connections={d['max_connections']} is very high")
    return {"database": db, "parameters": params, "risky_settings": risky}


def get_system_health(args):
    db = _resolve_db(args)
    q = lambda sql, p=None: _q(db, sql, p)
    try: bgw = q("SELECT buffers_clean, maxwritten_clean, buffers_alloc FROM pg_stat_bgwriter")
    except: bgw = []
    try: ckpt = q("SELECT num_timed as checkpoints_timed, num_requested as checkpoints_req, write_time, sync_time, buffers_written FROM pg_stat_checkpointer")
    except:
        try: ckpt = q("SELECT checkpoints_timed, checkpoints_req, checkpoint_write_time as write_time, checkpoint_sync_time as sync_time, buffers_checkpoint as buffers_written FROM pg_stat_bgwriter")
        except: ckpt = []
    cache = q("SELECT sum(blks_hit) as hits, sum(blks_read) as reads, CASE WHEN sum(blks_hit)+sum(blks_read)>0 THEN round(100.0*sum(blks_hit)/(sum(blks_hit)+sum(blks_read)),2) ELSE 100 END as hit_ratio_pct FROM pg_stat_database WHERE datname=current_database()")
    temp = q("SELECT temp_files, pg_size_pretty(temp_bytes) as temp_size FROM pg_stat_database WHERE datname=current_database()")
    txid = q("SELECT datname, age(datfrozenxid) as xid_age, round(100.0*age(datfrozenxid)/current_setting('autovacuum_freeze_max_age')::bigint,1) as pct_towards_wraparound FROM pg_database WHERE datname=current_database()")
    # Per-table frozen XID ages (top offenders — includes system catalogs)
    txid_per_table = q("SELECT c.oid::regclass::text as relname, n.nspname as schema, age(c.relfrozenxid) as xid_age, round(100.0*age(c.relfrozenxid)/current_setting('autovacuum_freeze_max_age')::bigint,1) as pct_towards_wraparound, pg_size_pretty(pg_total_relation_size(c.oid)) as table_size FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','t','m') AND c.relfrozenxid != '0' ORDER BY age(c.relfrozenxid) DESC LIMIT 20")
    try: repl = q("SELECT client_addr, state, pg_wal_lsn_diff(sent_lsn, replay_lsn) as replay_lag_bytes, pg_size_pretty(pg_wal_lsn_diff(sent_lsn, replay_lsn)) as replay_lag_pretty, write_lag, flush_lag, replay_lag FROM pg_stat_replication")
    except: repl = []
    # Replication slots (can prevent WAL cleanup)
    try: repl_slots = q("SELECT slot_name, slot_type, active, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) as retained_wal FROM pg_replication_slots")
    except: repl_slots = []
    # Database size breakdown (top 10 tables by size)
    db_size = q("SELECT n.nspname as schema, c.relname, pg_size_pretty(pg_total_relation_size(c.oid)) as total_size, pg_total_relation_size(c.oid) as size_bytes FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='r' AND n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 10")
    # Stats freshness
    try: stats_age = q("SELECT stats_reset, age(clock_timestamp(), stats_reset) as stats_age FROM pg_stat_database WHERE datname=current_database()")
    except: stats_age = []
    issues = []
    if cache and float(cache[0].get("hit_ratio_pct", 100)) < 95: issues.append(f"Buffer cache hit ratio {cache[0]['hit_ratio_pct']}%")
    if txid and float(txid[0].get("pct_towards_wraparound", 0)) > 50: issues.append(f"Txid wraparound risk: {txid[0]['pct_towards_wraparound']}%")
    if temp and int(temp[0].get("temp_files", 0)) > 0: issues.append(f"Temp files in use: {temp[0]['temp_size']}")
    if repl_slots:
        inactive = [s for s in repl_slots if not s.get("active")]
        if inactive: issues.append(f"{len(inactive)} inactive replication slot(s) retaining WAL")
    return {"database": db, "buffer_cache": cache, "bgwriter": bgw, "checkpointer": ckpt, "temp_files": temp, "txid_wraparound": txid, "txid_per_table": txid_per_table, "replication": repl, "replication_slots": repl_slots, "database_size_top_tables": db_size, "stats_age": stats_age, "issues": issues}


def get_connection_breakdown(args):
    db = _resolve_db(args)
    q = lambda sql, p=None: _q(db, sql, p)
    by_state = q("SELECT state, count(*) as cnt FROM pg_stat_activity WHERE backend_type='client backend' GROUP BY state ORDER BY cnt DESC")
    by_app = q("SELECT application_name, count(*) as cnt FROM pg_stat_activity WHERE backend_type='client backend' AND application_name!='' GROUP BY application_name ORDER BY cnt DESC LIMIT 15")
    by_user = q("SELECT usename, count(*) as cnt FROM pg_stat_activity WHERE backend_type='client backend' GROUP BY usename ORDER BY cnt DESC")
    total = q("SELECT count(*) as total FROM pg_stat_activity WHERE backend_type='client backend'")
    max_c = q("SELECT setting::int as max_connections FROM pg_settings WHERE name='max_connections'")
    t_val, m_val = int(total[0]["total"]) if total else 0, int(max_c[0]["max_connections"]) if max_c else 100
    issues = []
    if t_val > m_val * 0.8: issues.append(f"Connection usage at {round(100*t_val/m_val)}%")
    return {"database": db, "total": t_val, "max": m_val, "utilization_pct": round(100*t_val/m_val, 1), "by_state": by_state, "by_application": by_app, "by_user": by_user, "issues": issues}


def get_autovacuum_workers_status(args):
    db = _resolve_db(args)
    q = lambda sql, p=None: _q(db, sql, p)
    workers = q("SELECT pid, query, age(clock_timestamp(), xact_start) as duration, wait_event_type, wait_event FROM pg_stat_activity WHERE query LIKE 'autovacuum:%'")
    try: progress = q("SELECT p.pid, a.query, p.phase, p.heap_blks_total, p.heap_blks_vacuumed, CASE WHEN p.heap_blks_total>0 THEN round(100.0*p.heap_blks_vacuumed/p.heap_blks_total,1) ELSE 0 END as pct_complete FROM pg_stat_progress_vacuum p JOIN pg_stat_activity a ON a.pid=p.pid")
    except: progress = []
    mw_rows = q("SELECT setting FROM pg_settings WHERE name='autovacuum_max_workers'")
    mw = int(mw_rows[0]["setting"]) if mw_rows else 3
    return {"database": db, "active_workers": len(workers), "max_workers": mw, "workers": workers, "progress": progress, "all_slots_busy": len(workers) >= mw, "interpretation": f"{len(workers)}/{mw} workers active" + (" — all slots busy" if len(workers) >= mw else "")}


def generate_diagnosis_report(args):
    db = _resolve_db(args)
    ctx = args.get("incident_context", "General performance issue")
    a = {"database": db}
    perf = diagnose_database_performance(a)
    sessions = get_active_sessions_and_locks(a)
    waits = get_wait_event_analysis(a)
    config = get_database_configuration(a)
    system = get_system_health(a)
    av = get_autovacuum_workers_status(a)
    conns = get_connection_breakdown(a)
    issues = []
    if perf.get("severity") in ("high", "medium"): issues.append(f"Performance severity: {perf['severity']}")
    if sessions.get("blocking_detected"): issues.append("Active blocking detected")
    if waits.get("top_waits"): issues.extend([w.get("wait_event", "") for w in waits["top_waits"][:3]])
    issues.extend(config.get("risky_settings", []))
    issues.extend(system.get("issues", []))
    issues.extend(conns.get("issues", []))
    if av.get("all_slots_busy"): issues.append("All autovacuum slots busy")
    return {"database": db, "executive_summary": f"Diagnosis for: {ctx}", "severity": perf.get("severity", "unknown"), "issues_found": issues, "evidence": {"performance": perf, "sessions": sessions, "waits": waits, "config": config, "system": system, "autovacuum": av, "connections": conns}, "requires_human_approval": ["VACUUM FULL", "Index creation on large tables", "Config changes requiring restart", "Killing sessions"]}


# ── Dispatch + MCP Handler ──────────────────────────────────────────────────
DISPATCH = {t["name"]: globals()[t["name"]] for t in TOOLS}


def call_tool(name, args):
    fn = DISPATCH.get(name)
    if not fn: return [{"type": "text", "text": f"Unknown tool: {name}"}]
    try:
        return [{"type": "text", "text": _j(fn(args))}]
    except Exception as e:
        return [{"type": "text", "text": f"Error: {type(e).__name__}: {e}\n{traceback.format_exc()[-500:]}", "isError": True}]


def handle_rpc(req):
    rid, m = req.get("id", 1), req.get("method", "")
    if m == "initialize": return {"jsonrpc": "2.0", "id": rid, "result": INIT_RESULT}
    if m == "notifications/initialized": return None
    if m == "tools/list": return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if m == "tools/call":
        p = req.get("params", {})
        return {"jsonrpc": "2.0", "id": rid, "result": {"content": call_tool(p.get("name", ""), p.get("arguments", {}))}}
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Unknown method: {m}"}}


class MCPHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._respond({"jsonrpc": "2.0", "id": 1, "result": INIT_RESULT})
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        try: req = json.loads(raw) if raw else {}
        except: req = {}
        resp = handle_rpc(req)
        if resp is None:
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); return
        self._respond(resp)
    def _respond(self, body):
        data = json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(data)
    def log_message(self, fmt, *args): print(f"[pg-diagnose] {fmt % args}")


if __name__ == "__main__":
    _discover_databases()
    threading.Thread(target=_refresh_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "8000"))
    print(f"[pg-diagnose] Starting MCP server on 0.0.0.0:{port}")
    HTTPServer(("0.0.0.0", port), MCPHandler).serve_forever()
