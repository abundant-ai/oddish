from __future__ import annotations

import os
from urllib.parse import quote

import asyncpg
import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool

_MAX_CHARS = 12000
_LOG_TAIL_CHARS = 6000

# Read-only SQL guardrails. The Postgres READ ONLY transaction (below) is the
# real enforcement; these just bound blast radius and give clean errors.
_SQL_MAX_ROWS = 200
_SQL_MAX_CELL_CHARS = 200
_SQL_STATEMENT_TIMEOUT_MS = 15000
_SQL_CONNECT_TIMEOUT = 10
_SQL_ALLOWED_LEADERS = ("select", "with", "explain", "show", "table", "values")


def _cfg() -> tuple[str, dict]:
    return (
        os.environ["ODDISH_API_URL"].rstrip("/"),
        {"Authorization": f"Bearer {os.environ['ODDISH_API_KEY']}"},
    )


def _text(s: str) -> dict:
    if len(s) > _MAX_CHARS:
        s = s[:_MAX_CHARS] + f"\n… [truncated, {len(s)} chars total]"
    return {"content": [{"type": "text", "text": s}]}


def _window(days) -> str:
    return "all-time" if not days else f"last {days} days"


async def _get(path: str, params: dict | None = None) -> dict:
    url, headers = _cfg()
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(f"{url}{path}", headers=headers, params=params)
        r.raise_for_status()
        return r.json()


@tool(
    "oddish_costs",
    "Global cost/spend breakdown across all orgs, users, models, and experiments. "
    "Optional window_days (default 7, 0=all-time).",
    {
        "type": "object",
        "properties": {"window_days": {"type": "integer"}},
        "required": [],
    },
)
async def oddish_costs(args: dict) -> dict:
    data = await _get("/admin/costs", {"window_days": args.get("window_days", 7)})
    t = data.get("totals", {})
    lines = [
        f"*Cost totals* ({_window(data.get('window_days'))})",
        f"• total spend: ${t.get('cost_usd', 0):,.2f}",
        f"• trials: {t.get('trial_count', 0):,}  users: {t.get('user_count', 0)}  "
        f"experiments: {t.get('experiment_count', 0)}",
        "",
        "*Top users by spend*",
    ]
    for u in data.get("by_user", [])[:10]:
        label = u.get("name") or u.get("email") or u.get("label") or u.get("key") or "?"
        org = u.get("org_name") or u.get("org_id") or ""
        lines.append(f"• {label} ({org}): ${u.get('cost_usd', 0):,.2f}, {u.get('trial_count', 0)} trials")
    lines.append("")
    lines.append("*Top models by spend*")
    for m in data.get("by_model", [])[:8]:
        lines.append(f"• {m.get('model')} ({m.get('provider')}): ${m.get('cost_usd', 0):,.2f}")
    return _text("\n".join(lines))


@tool(
    "oddish_user_costs",
    "Per-user cost breakdown by task and model for a specific user_id. "
    "Optional window_days (default 7, 0=all-time).",
    {
        "type": "object",
        "properties": {
            "user_id": {"type": "string"},
            "window_days": {"type": "integer"},
        },
        "required": ["user_id"],
    },
)
async def oddish_user_costs(args: dict) -> dict:
    uid = args["user_id"]
    try:
        data = await _get(
            f"/admin/costs/users/{quote(uid, safe='')}",
            {"window_days": args.get("window_days", 7)},
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return _text(f"No user found for id `{uid}`.")
        raise
    t = data.get("totals", {})
    who = data.get("name") or data.get("email") or data.get("github_username") or uid
    lines = [
        f"*Costs for {who}* ({_window(data.get('window_days'))})",
        f"• total: ${t.get('cost_usd', 0):,.2f}  trials: {t.get('trial_count', 0):,}  "
        f"tasks: {t.get('task_count', 0)}",
        "",
        "*Top tasks*",
    ]
    for task in sorted(data.get("tasks", []), key=lambda x: x.get("cost_usd", 0), reverse=True)[:10]:
        lines.append(
            f"• {task.get('task_name') or task.get('task_id')}: "
            f"${task.get('cost_usd', 0):,.2f}, {task.get('trial_count', 0)} trials"
        )
    return _text("\n".join(lines))


@tool(
    "oddish_queue_health",
    "Queue health: throughput, per-queue capacity/fill, wait percentiles, dispatcher/reconciler heartbeat.",
    {"type": "object", "properties": {}, "required": []},
)
async def oddish_queue_health(args: dict) -> dict:
    data = await _get("/admin/queue-health")
    lines = [
        f"*Queue health*  queued: {data.get('totals_queued', 0)}  running: {data.get('totals_running', 0)}",
    ]
    if data.get("throughput"):
        lines += ["", "*Throughput (started/finished)*"]
        for t in data["throughput"]:
            lines.append(
                f"• {t.get('kind')}: 5m {t.get('started_5m', 0)}/{t.get('finished_5m', 0)}, "
                f"15m {t.get('started_15m', 0)}/{t.get('finished_15m', 0)}, "
                f"60m {t.get('started_60m', 0)}/{t.get('finished_60m', 0)}"
            )
    lines += ["", "*Capacity (most pressured first)*"]
    for c in data.get("capacity", [])[:12]:
        fill = c.get("fill")
        fill_s = f"{fill * 100:.0f}%" if fill is not None else "?"
        p95 = c.get("wait_p95_seconds")
        p95_s = f"{p95:.0f}s" if p95 is not None else "—"
        lines.append(
            f"• {c.get('queue_key')}: {c.get('running', 0)}/{c.get('limit', 0)} running "
            f"({fill_s}), {c.get('queued', 0)} queued, p95 wait {p95_s}"
        )
    for name in ("dispatcher", "reconciler"):
        comp = data.get(name)
        if comp:
            age = comp.get("age_seconds")
            lines.append(f"• {name}: last beat {age:.0f}s ago" if age is not None else f"• {name}: no heartbeat")
    return _text("\n".join(lines))


@tool(
    "oddish_trial_logs",
    "Structured logs for a trial (agent commands, verifier stdout/stderr, exception). "
    "Use to diagnose why a trial failed.",
    {
        "type": "object",
        "properties": {"trial_id": {"type": "string"}},
        "required": ["trial_id"],
    },
)
async def oddish_trial_logs(args: dict) -> dict:
    tid = args["trial_id"]
    try:
        data = await _get(f"/trials/{quote(tid, safe='')}/logs/structured")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return _text(
                f"No trial found for id `{tid}` in this bot's org. Trial-log lookups are "
                "org-scoped, so a trial that shows up in global cost/SQL data can still be "
                "missing here if it belongs to another org."
            )
        raise
    parts = [f"*Trial {data.get('trial_id')}*"]
    if data.get("exception"):
        parts.append(f"\n*exception*\n{data['exception'][-_LOG_TAIL_CHARS:]}")
    verifier = data.get("verifier") or {}
    for k in ("stderr", "stdout"):
        if verifier.get(k):
            parts.append(f"\n*verifier {k}*\n{verifier[k][-_LOG_TAIL_CHARS:]}")
    agent = data.get("agent") or {}
    for cmd in (agent.get("commands") or [])[-4:]:
        parts.append(f"\n*{cmd.get('name')}*\n{(cmd.get('content') or '')[-2000:]}")
    return _text("\n".join(parts))


@tool(
    "oddish_tasks",
    "List tasks (evals) with status and progress. Optional status, user, experiment_id filters and limit.",
    {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "user": {"type": "string"},
            "experiment_id": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "required": [],
    },
)
async def oddish_tasks(args: dict) -> dict:
    params = {"compact_tasks": True, "include_worker_jobs": False, "limit": args.get("limit") or 25}
    for k in ("status", "user", "experiment_id"):
        if args.get(k):
            params[k] = args[k]
    tasks = await _get("/tasks", params)
    lines = [f"*Tasks* ({len(tasks)})"]
    for t in tasks:
        lines.append(
            f"• `{t.get('id')}` {t.get('name')} [{t.get('status')}] "
            f"{t.get('progress')} — {t.get('experiment_name')} ({t.get('user')})"
        )
    return _text("\n".join(lines))


def _sql_url() -> str:
    """DSN for the read-only SQL tool.

    Prefer ``ODDISH_DATABASE_URL_RO`` (a credential for a Postgres role granted
    only SELECT) when present; fall back to ``ODDISH_DATABASE_URL``. Either way
    every query runs inside a Postgres ``READ ONLY`` transaction, so writes are
    rejected by the server even on a read/write role -- the dedicated RO role is
    defense in depth, not the sole guarantee. ``asyncpg`` speaks the wire
    protocol directly and does not understand SQLAlchemy's ``+asyncpg`` driver
    tag, so strip it (matching ``scripts/backfill_analysis.py``).
    """
    url = os.environ.get("ODDISH_DATABASE_URL_RO") or os.environ["ODDISH_DATABASE_URL"]
    return url.replace("+asyncpg", "")


def _check_readonly_sql(sql: str) -> str | None:
    """Return an error string if ``sql`` is not a single read-only statement.

    Defense in depth in front of the READ ONLY transaction: reject stacked
    statements and anything that does not begin with a read-only leader, so
    obvious writes fail with a clear message instead of a Postgres error.
    """
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        return "Empty query."
    if ";" in stripped:
        return "Only a single statement is allowed (no `;`-separated statements)."
    leader = stripped.split(None, 1)[0].lower()
    if leader not in _SQL_ALLOWED_LEADERS:
        return (
            f"Only read-only queries are allowed (must start with one of "
            f"{', '.join(_SQL_ALLOWED_LEADERS).upper()}); got `{leader.upper()}`."
        )
    return None


def _cell(v) -> str:
    """One table cell: never None, single-line, and length-bounded.

    A single wide field (JSON blob, log line) would otherwise blow the whole
    12k ``_text`` budget and could truncate mid-fence, so cap each cell and
    strip newlines that would break the Slack code block's row alignment.
    """
    if v is None:
        return ""
    s = str(v).replace("\n", " ").replace("\r", " ")
    return s if len(s) <= _SQL_MAX_CELL_CHARS else s[: _SQL_MAX_CELL_CHARS - 1] + "…"


def _format_rows(records: list[asyncpg.Record], truncated: bool) -> str:
    if not records:
        return "_(0 rows returned)_"
    cols = list(records[0].keys())
    lines = [" | ".join(cols), " | ".join("---" for _ in cols)]
    for rec in records:
        lines.append(" | ".join(_cell(v) for v in rec.values()))
    header = f"*{len(records)} row(s)*" + (
        f" _(capped at {_SQL_MAX_ROWS}; aggregate in SQL or add a tighter WHERE/LIMIT)_"
        if truncated
        else ""
    )
    return header + "\n```\n" + "\n".join(lines) + "\n```"


@tool(
    "oddish_sql",
    "Run a READ-ONLY SQL query against the oddish Postgres and get rows back. "
    "Use for questions the other tools don't cover -- e.g. break down QA/analysis "
    "cost from `analysis_costs`, join trials to tasks, aggregate spend by day. "
    "Only SELECT/WITH/EXPLAIN/SHOW is allowed; the query runs in a READ ONLY "
    "transaction with a 15s statement timeout and at most 200 rows are returned. "
    "Raw SQL bypasses the app's soft-delete filter, so add `WHERE deleted_at IS "
    "NULL` on tables that have it. List a table's columns with `select "
    "column_name, data_type from information_schema.columns where "
    "table_name='<table>' order by ordinal_position`, or list tables with "
    "`select table_name from information_schema.tables where table_schema='public'`.",
    {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
)
async def oddish_sql(args: dict) -> dict:
    sql = args.get("query") or ""
    err = _check_readonly_sql(sql)
    if err:
        return _text(f":no_entry: {err}")
    try:
        conn = await asyncpg.connect(
            _sql_url(), statement_cache_size=0, timeout=_SQL_CONNECT_TIMEOUT
        )
    except Exception as e:  # noqa: BLE001 -- surface connection failures to Slack
        return _text(f":warning: Could not connect to the database: {type(e).__name__}: {e}")
    try:
        # readonly=True makes Postgres itself reject any write (INSERT/UPDATE/DDL/
        # etc.) with "cannot execute ... in a read-only transaction" -- the actual
        # guarantee behind this tool. SET LOCAL scopes the timeout to this txn.
        async with conn.transaction(readonly=True):
            await conn.execute(f"SET LOCAL statement_timeout = {_SQL_STATEMENT_TIMEOUT_MS}")
            cur = await conn.cursor(sql.strip().rstrip(";"))
            records = await cur.fetch(_SQL_MAX_ROWS + 1)
    except asyncpg.PostgresError as e:
        return _text(f":warning: Query error: {type(e).__name__}: {e}")
    finally:
        await conn.close()
    truncated = len(records) > _SQL_MAX_ROWS
    return _text(_format_rows(records[:_SQL_MAX_ROWS], truncated))


SERVER_NAME = "oddish"

_TOOLS = [
    oddish_costs,
    oddish_user_costs,
    oddish_queue_health,
    oddish_trial_logs,
    oddish_tasks,
    oddish_sql,
]


def build_server():
    return create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=_TOOLS,
    )


def allowed_tool_names() -> list[str]:
    """Fully-qualified MCP names (``mcp__<server>__<tool>``) for every tool.

    Under ``permission_mode="dontAsk"`` the agent denies any tool not listed in
    ``allowed_tools``, so this must enumerate the exact tool names rather than a
    wildcard (wildcards are not an SDK-supported allow pattern).
    """
    return [f"mcp__{SERVER_NAME}__{t.name}" for t in _TOOLS]
