from __future__ import annotations

import logging
import os
import re

import httpx
from oddish.timing import RequestTimedAsyncClient

log = logging.getLogger("oddish.carl")

_DEFAULT_URL = "https://costs.abundant.run"
_RANGES = {
    "1d",
    "7d",
    "14d",
    "30d",
    "60d",
    "90d",
    "180d",
    "365d",
    "730d",
    "1095d",
}
_PROVIDERS = {
    "all",
    "aws",
    "anthropic",
    "openai",
    "gcp",
    "azure",
    "modal",
    "daytona",
    "thunder",
    "oddish",
}
_GROUPS = ("provider", "model", "project", "owner", "key")
_COST_KINDS = ("compute", "token")
_PNG_MAGIC = b"\x89PNG"
_CHART_FILENAME_RE = re.compile(r'filename="([^"]+\.png)"')
_pending_charts: list[tuple[bytes, str, str]] = []


def _money(n: object) -> str:
    try:
        return f"${float(n):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def _delta(percent: object) -> str:
    try:
        value = float(percent)
    except (TypeError, ValueError):
        return "n/a"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.1f}%"


def catfish_config() -> tuple[str, dict[str, str]] | str:
    """Return (base_url, headers) or a Slack-ready reason we cannot call Catfish."""
    url = os.environ.get("CATFISH_API_URL", _DEFAULT_URL).strip() or _DEFAULT_URL
    token = os.environ.get("CATFISH_API_TOKEN", "").strip()
    if not token:
        return (
            "CATFISH_API_TOKEN is not set; cannot read the cloud bill. "
            "This is a service token on Catfish, not the Neon password. "
            "After adding it to oddish-prod, recycle only `carl_answer`."
        )
    headers = {"Authorization": f"Bearer {token}"}
    bypass = (
        os.environ.get("CATFISH_VERCEL_BYPASS", "").strip()
        or os.environ.get("VERCEL_AUTOMATION_BYPASS_SECRET", "").strip()
    )
    if bypass:
        headers["x-vercel-protection-bypass"] = bypass
    return url.rstrip("/"), headers


def catfish_query_params(args: dict, *, default_group: str) -> dict[str, str] | str:
    range_name = str(args.get("range") or "7d").strip()
    if range_name == "24h":
        range_name = "1d"
    start = str(args.get("start") or "").strip()
    end = str(args.get("end") or "").strip()
    provider = str(args.get("provider") or "all").strip().lower()
    group = str(args.get("group") or default_group).strip().lower()
    cost_kind = str(args.get("cost_kind") or args.get("costKind") or "").strip().lower()

    if not start and range_name not in _RANGES:
        return f"Unknown range `{range_name}`. Use 1d, 7d, 14d, 30d, …"
    if provider not in _PROVIDERS:
        return f"Unknown provider `{provider}`."
    if group not in _GROUPS:
        return f"Unknown group `{group}`. Use {' / '.join(_GROUPS)}."
    if cost_kind and cost_kind not in _COST_KINDS:
        return f"Unknown cost_kind `{cost_kind}`. Use compute or token."

    params: dict[str, str] = {"provider": provider, "group": group}
    if start and end:
        params["start"] = start
        params["end"] = end
    elif start:
        params["start"] = start
    else:
        params["range"] = range_name
    if cost_kind:
        params["costKind"] = cost_kind
    return params


async def _catfish_get(path: str, params: dict[str, str]) -> httpx.Response | str:
    cfg = catfish_config()
    if isinstance(cfg, str):
        return cfg
    url, headers = cfg
    try:
        async with RequestTimedAsyncClient(timeout=60) as client:
            response = await client.get(f"{url}{path}", headers=headers, params=params)
            response.raise_for_status()
            return response
    except httpx.HTTPStatusError as exc:
        body = (exc.response.text or "").strip()[:300]
        return f"Catfish HTTP {exc.response.status_code}: {body or exc}"
    except httpx.HTTPError as exc:
        return f"Could not reach Catfish: {type(exc).__name__}: {exc}"


async def fetch_catfish_costs(params: dict[str, str]) -> dict | str:
    response = await _catfish_get("/api/carl/costs", params)
    if isinstance(response, str):
        return response
    return response.json()


def _chart_filename(response: httpx.Response) -> str:
    header = response.headers.get("content-disposition") or ""
    match = _CHART_FILENAME_RE.search(header)
    name = match.group(1) if match else "catfish-mix.png"
    if "/" in name or "\\" in name:
        return "catfish-mix.png"
    return name


async def fetch_catfish_chart(params: dict[str, str]) -> tuple[bytes, str] | str:
    """Same auth and query as costs; PNG of the daily provider mix."""
    response = await _catfish_get("/api/carl/chart", params)
    if isinstance(response, str):
        return response
    body = response.content
    if not body.startswith(_PNG_MAGIC):
        return "Catfish chart was not a PNG"
    return body, _chart_filename(response)


def queue_catfish_chart(png: bytes, caption: str, filename: str = "catfish-mix.png") -> None:
    _pending_charts.append((png, caption, filename))


def drain_catfish_charts() -> list[tuple[bytes, str, str]]:
    items = list(_pending_charts)
    _pending_charts.clear()
    return items


async def maybe_queue_catfish_chart(params: dict[str, str], caption: str) -> None:
    """Proof PNG for a successful text answer. Failures stay off the Slack reply."""
    try:
        chart = await fetch_catfish_chart(params)
    except Exception:
        log.exception("catfish chart fetch failed")
        return
    if isinstance(chart, str):
        log.info("catfish chart skipped: %s", chart)
        return
    png, filename = chart
    queue_catfish_chart(png, caption, filename)


def _window_label(data: dict) -> str:
    query = data.get("query") or {}
    provider = query.get("provider") or "all"
    who = "all providers" if provider == "all" else str(provider)
    if query.get("range") and query.get("range") != "custom":
        span = str(query["range"])
    else:
        span = f"{query.get('startDate')} → {query.get('endDate')}"
    kind = query.get("costKind")
    extra = f", {kind}" if kind else ""
    return f"{who}, {span}{extra}"


def _coverage_lines(data: dict) -> list[str]:
    gaps = data.get("coverage") or []
    if not gaps:
        return []
    lines = ["", "*Coverage*"]
    for gap in gaps:
        note = gap.get("note") or gap.get("provider") or "pending"
        lines.append(
            f"• {note} — that provider's day is not in yet; do not treat a "
            "missing day as $0"
        )
    return lines


def _view_lines(data: dict) -> list[str]:
    view = data.get("view")
    if not isinstance(view, str) or not view.startswith("https://"):
        return []
    # Bare URL: `_deliver` HTML-escapes the answer, so Slack `<url|label>`
    # would render as literal angle brackets.
    return ["", f"Open this view in Catfish: {view}"]


def format_catfish_costs(data: dict) -> str:
    totals = data.get("totals") or {}
    lines = [
        f"*Catfish cloud spend* ({_window_label(data)})",
        f"• total: {_money(totals.get('usd'))}  prior: "
        f"{_money(totals.get('previousUsd'))}  ({_delta(totals.get('changePercent'))})",
    ]
    lines += _coverage_lines(data)

    series = data.get("series") or []
    if series:
        lines += ["", "*Daily*"]
        for point in series:
            lines.append(f"• {point.get('date')}: {_money(point.get('usd'))}")
    lines += _view_lines(data)
    return "\n".join(lines)


def format_catfish_breakdown(data: dict) -> str:
    query = data.get("query") or {}
    group = query.get("group") or "provider"
    totals = data.get("totals") or {}
    lines = [
        f"*Catfish breakdown by {group}* ({_window_label(data)})",
        f"• total: {_money(totals.get('usd'))}  prior: "
        f"{_money(totals.get('previousUsd'))}  ({_delta(totals.get('changePercent'))})",
    ]
    lines += _coverage_lines(data)
    rows = data.get("breakdown") or []
    if rows:
        lines += ["", f"*Top {group}s*"]
        for row in rows[:15]:
            label = row.get("label") or row.get("key") or "?"
            extra = row.get("provider") or ""
            suffix = f" ({extra})" if extra and extra != label else ""
            lines.append(
                f"• {label}{suffix}: {_money(row.get('usd'))}  "
                f"prior {_money(row.get('previousUsd'))}  "
                f"({_delta(row.get('changePercent'))})"
            )
    lines += _view_lines(data)
    return "\n".join(lines)
