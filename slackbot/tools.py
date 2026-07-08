from __future__ import annotations

import os
from urllib.parse import quote

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool

_MAX_CHARS = 12000
_LOG_TAIL_CHARS = 6000


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
        "",
        "*Capacity (most pressured first)*",
    ]
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
            return _text(f"No trial found for id `{tid}`.")
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
    params = {"compact_tasks": True, "include_worker_jobs": False, "limit": args.get("limit", 25)}
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


SERVER_NAME = "oddish"

_TOOLS = [
    oddish_costs,
    oddish_user_costs,
    oddish_queue_health,
    oddish_trial_logs,
    oddish_tasks,
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
