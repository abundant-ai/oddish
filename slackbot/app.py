from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time

import modal

SECRET_NAME = "oddish-slackbot"
MODEL = "claude-opus-4-8"
_MAX_SLACK = 3900
_ANSWER_TIMEOUT = 1800
_ANSWER_DEADLINE = 1700
# The webhook only verifies the signature, acks, and schedules the real work as
# a background task, so a single warm container can fan out many mentions.
_WEB_MAX_INPUTS = 100
_WEB_TARGET_INPUTS = 80

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("oddish-slackbot")

# The Claude Code CLI that ``claude-agent-sdk`` drives is bundled inside the
# SDK wheel (a self-contained, per-architecture binary), so there is no
# separate Node/npm/CLI install step -- just pip install the SDK.
image = (
    modal.Image.debian_slim(python_version="3.13")
    .apt_install("ca-certificates")
    .pip_install(
        "claude-agent-sdk==0.2.110",
        "fastapi[standard]",
        "slack_sdk",
        "httpx",
        "asyncpg",
    )
    .add_local_python_source("tools")
)

app = modal.App("oddish-slackbot")
secret = modal.Secret.from_name(SECRET_NAME)
seen_events = modal.Dict.from_name("oddish-slackbot-seen-events", create_if_missing=True)
watch_state = modal.Dict.from_name("oddish-slackbot-watch-state", create_if_missing=True)

SYSTEM_PROMPT = (
    "You answer teammates' questions about the oddish eval platform (spend, quotas, "
    "queue health, why a trial failed) using the provided read-only tools. Be concise. "
    "Format for Slack mrkdwn (*bold*, `code`, • bullets; never markdown headings or tables). "
    "Always include concrete numbers. Call tools rather than guessing. Today's date and all "
    "live data come from the tools and environment."
)


def _dashboard_url() -> str:
    return os.environ.get("ODDISH_DASHBOARD_URL", "https://www.oddish.app").rstrip("/")


def _prompt_extras() -> str:
    dash = _dashboard_url()
    return (
        " For aggregates or filters the fixed tools don't cover, call oddish_list_views "
        "then oddish_run_sql. "
        "End answers with the relevant dashboard link when one exists: experiment "
        f"{dash}/experiments/<id percent-encoded twice>, task {dash}/tasks/<id>, admin "
        f"costs and quotas {dash}/admin. Trials have no page of their own; link the "
        "parent task or experiment. "
        "You cannot change quotas or anything else (the backend rejects API-key writes); "
        f"for a quota change link {dash}/admin and name the Quotas tab. "
        "When a question turns into an investigation (digging through code or many "
        "logs), point at the Chat button on that task/experiment page instead."
    )


def _log(event: str, **fields) -> None:
    log.info("%s %s", event, " ".join(f"{k}={v}" for k, v in fields.items() if v is not None))


def _strip_mention(text: str, bot_user_id: str | None) -> str:
    if bot_user_id:
        text = re.sub(rf"^\s*<@{re.escape(bot_user_id)}(?:\|[^>]*)?>\s*", "", text)
    return text.strip()


def _bot_user_id(payload: dict, event: dict) -> str | None:
    authorizations = payload.get("authorizations") or []
    # Prefer the authorization Slack flags as the bot; only fall back to the
    # first entry (or the leading mention in the text) when none is flagged,
    # so multi-authorization payloads don't strip the wrong user id.
    for auth in authorizations:
        if auth.get("is_bot") and auth.get("user_id"):
            return auth["user_id"]
    for auth in authorizations:
        uid = auth.get("user_id")
        if uid:
            return uid
    m = re.match(r"\s*<@([A-Z0-9]+)(?:\|[^>]*)?>", event.get("text", ""))
    return m.group(1) if m else None


def _claim_event(event_id: str | None) -> bool:
    if not event_id:
        return True
    return seen_events.put(event_id, time.time(), skip_if_exists=True)


def _release_event(event_id: str | None) -> None:
    """Undo a claim so a Slack redelivery of the event is reprocessed.

    Called when work fails *after* claiming (spawn error, or the answer
    worker cannot even post its placeholder) so the event is not left
    marked "seen" with no visible bot response.
    """
    if event_id:
        try:
            seen_events.pop(event_id, None)
        except Exception:
            log.exception("failed to release event claim event_id=%s", event_id)


def _verify_slack(headers: dict[str, str], body: bytes) -> bool:
    ts = headers.get("x-slack-request-timestamp", "")
    sig = headers.get("x-slack-signature", "")
    if not ts.isdigit() or not sig or abs(time.time() - int(ts)) > 300:
        return False
    base = b"v0:" + ts.encode() + b":" + body
    mac = "v0=" + hmac.new(os.environ["SLACK_SIGNING_SECRET"].encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, sig)


def _dispatch(payload: dict) -> None:
    event_id = payload.get("event_id")
    channel: str | None = None
    thread: str | None = None
    claimed = False
    handed_off = False
    try:
        # Only answer events from the expected workspace when one is configured.
        # Fails open if SLACK_TEAM_ID is unset so single-workspace installs need
        # no extra plumbing; when set, events from any other team are dropped.
        expected_team = os.environ.get("SLACK_TEAM_ID", "").strip()
        if expected_team and payload.get("team_id") != expected_team:
            _log("wrong_workspace", event_id=event_id, team_id=payload.get("team_id"))
            return
        event = payload.get("event") or {}
        if event.get("type") != "app_mention" or event.get("bot_id"):
            return
        # Ignore edits/deletions of an existing mention; only act on the original.
        if event.get("subtype") or event.get("edited"):
            return
        channel = event.get("channel")
        ts = event.get("ts")
        if not channel or not ts:
            _log("malformed_event", event_id=event_id)
            return
        thread = event.get("thread_ts") or ts
        user = event.get("user")
        _log("received", event_id=event_id, channel=channel, thread=thread, user=user)
        # Claim up front so *every* outcome -- including the rejection replies
        # below -- is deduplicated and a Slack redelivery of the same event_id
        # is never answered twice. The outer handler releases the claim if we
        # fail before successfully handing the event to the worker, so a
        # transient error still leaves the redelivery free to retry.
        if not _claim_event(event_id):
            _log("duplicate", event_id=event_id)
            return
        claimed = True

        allowed = os.environ.get("SLACK_ALLOWED_USERS", "").strip()
        if not allowed:
            _log("allowlist_unset", event_id=event_id, user=user)
            _notify(channel, thread, "This bot's allowlist is not configured; refusing to run.", event_id)
            return
        if not user or user not in {u.strip() for u in allowed.split(",") if u.strip()}:
            _log("unauthorized", event_id=event_id, user=user)
            _notify(channel, thread, "Sorry, you're not authorized to use this bot.", event_id)
            return

        cleaned = _strip_mention(event.get("text", ""), _bot_user_id(payload, event))
        if not cleaned:
            _log("empty_prompt", event_id=event_id, user=user)
            _notify(channel, thread, "Ask me a question after the mention, e.g. `@Oddish Claude why did trial abc123 fail?`", event_id)
            return
        answer.spawn(channel, thread, cleaned, user, event_id)
        handed_off = True
        _log("claimed", event_id=event_id, channel=channel, thread=thread, user=user)
    except Exception:
        # Runs as a fire-and-forget background task after the HTTP ack, so an
        # unhandled error would otherwise vanish into the server logs untraced.
        # Slack already got its 200 and will not retry on its own, so release
        # the dedup claim (only if we made one but never handed off the work)
        # and surface the failure in-thread instead of leaving the user with
        # no response at all.
        log.exception("dispatch failed event_id=%s", event_id)
        if claimed and not handed_off:
            _release_event(event_id)
        if channel and thread:
            try:
                _post(channel, thread, ":warning: I hit an error handling that mention. Please try again.")
            except Exception:
                log.exception("failed to post dispatch error event_id=%s", event_id)


@app.function(image=image, secrets=[secret], min_containers=1)
@modal.concurrent(target_inputs=_WEB_TARGET_INPUTS, max_inputs=_WEB_MAX_INPUTS)
@modal.asgi_app()
def web():
    from fastapi import BackgroundTasks, FastAPI, Request, Response

    api = FastAPI()

    @api.post("/slack/events")
    async def slack_events(request: Request, background: BackgroundTasks):
        body = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()}
        if not _verify_slack(headers, body):
            return Response(status_code=401)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            # Signature was valid but the body wasn't JSON. Ack with 400 rather
            # than 500 so Slack doesn't queue retries for a malformed request.
            return Response(status_code=400)
        if not isinstance(payload, dict):
            return Response(status_code=400)
        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge", "")}
        # Ack within Slack's 3s window and do the real work off the request path.
        background.add_task(_dispatch, payload)
        return Response(status_code=200)

    return api


def _client():
    from slack_sdk import WebClient
    from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=3))
    return client


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _split_at(text: str, limit: int) -> int:
    """Largest cut ``<= limit`` that never lands inside an ``&…;`` entity.

    ``_escape`` expands one source char into a multi-char run (``&amp;`` etc.),
    so a fixed slice can bisect an entity and render as broken text. Back the
    cut up out of any unterminated ``&`` (entities are at most 5 chars long).
    """
    if len(text) <= limit:
        return len(text)
    cut = limit
    amp = text.rfind("&", cut - 4, cut)
    if amp != -1 and ";" not in text[amp:cut]:
        cut = amp
    return cut


def _post(channel: str, thread: str | None, text: str) -> str:
    return _client().chat_postMessage(channel=channel, thread_ts=thread, text=text[:_split_at(text, _MAX_SLACK)])["ts"]


def _notify(channel: str, thread: str, text: str, event_id: str | None) -> None:
    """Best-effort terminal reply (rejection / hint).

    Swallows Slack errors so a failed post cannot bubble into the generic
    dispatch error handler and mask the specific message with a vague one.
    Releases the dedup claim on failure so a Slack redelivery can retry rather
    than leaving the user with no reply and the event stuck marked "seen".
    """
    try:
        _post(channel, thread, text)
    except Exception:
        log.exception("failed to post reply channel=%s", channel)
        _release_event(event_id)


def _update(channel: str, ts: str, text: str) -> None:
    _client().chat_update(channel=channel, ts=ts, text=text[:_split_at(text, _MAX_SLACK)])


def _deliver(channel: str, ts: str, thread: str, text: str) -> bool:
    """Replace the placeholder with the answer, spilling overflow into follow-ups.

    Returns ``True`` once the first chunk has been delivered — via the
    placeholder update, or a fresh thread post when that update fails.
    Overflow posts after the first chunk are best-effort: a failure there is
    logged but does not undo what the user already saw. Returns ``False`` only
    when the first chunk could not be delivered either way, so the caller can
    surface a fallback notice without overwriting a delivered answer.
    """
    text = _escape(text)
    cut = _split_at(text, _MAX_SLACK)
    try:
        _update(channel, ts, text[:cut])
    except Exception:
        log.exception("first delivery chunk failed channel=%s", channel)
        try:
            _post(channel, thread, text[:cut])
        except Exception:
            log.exception("first-chunk repost failed channel=%s", channel)
            return False
        try:
            _update(channel, ts, ":arrow_down: Answer posted below.")
        except Exception:
            log.exception("stale placeholder left channel=%s", channel)
    rest = text[cut:]
    while rest:
        cut = _split_at(rest, _MAX_SLACK)
        try:
            _post(channel, thread, rest[:cut])
        except Exception:
            log.exception("overflow delivery chunk failed channel=%s", channel)
            break
        rest = rest[cut:]
    return True


@app.function(image=image, secrets=[secret], timeout=_ANSWER_TIMEOUT)
async def answer(channel: str, thread: str, prompt: str, user: str | None, event_id: str | None) -> None:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        ToolUseBlock,
        query,
    )

    from tools import allowed_tool_names, build_server, set_audit_context

    _log("spawned", event_id=event_id, channel=channel, thread=thread, user=user)
    set_audit_context(user=user, channel=channel, thread=thread, event_id=event_id)
    try:
        status_ts = _post(channel, thread, "Thinking…")
    except Exception:
        # If we can't even post the placeholder we have nothing to update later,
        # so there's no way to surface an answer. Release the dedup claim so a
        # Slack redelivery of this event can be reprocessed instead of being
        # dropped as a duplicate with no visible bot activity.
        log.exception("initial post failed event_id=%s channel=%s", event_id, channel)
        _release_event(event_id)
        return
    try:
        budget = float(os.environ.get("SLACK_MAX_BUDGET_USD", "1.0"))
    except ValueError:
        budget = 1.0
    options = ClaudeAgentOptions(
        model=MODEL,
        mcp_servers={"oddish": build_server()},
        allowed_tools=allowed_tool_names(),
        # Structurally disable every built-in tool so only the enumerated
        # mcp__oddish__* tools exist, and refuse any MCP config the CLI would
        # otherwise auto-load -- defense in depth beyond permission_mode.
        tools=[],
        strict_mcp_config=True,
        permission_mode="dontAsk",
        max_turns=25,
        max_budget_usd=budget,
        system_prompt=SYSTEM_PROMPT + _prompt_extras(),
    )

    final = ""
    last_text = ""
    last_edit = 0.0
    cost = None
    hit_turn_limit = False
    hit_budget_limit = False
    status_ok = True
    partial_note = ""

    async def run() -> None:
        nonlocal final, last_text, last_edit, cost, hit_turn_limit, hit_budget_limit, status_ok
        try:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    status = None
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            status = f"_Running `{block.name.split('__')[-1]}`…_"
                        elif isinstance(block, TextBlock) and block.text.strip():
                            status = block.text.strip()
                            last_text = status
                    now = time.time()
                    if status and status_ok and now - last_edit > 2:
                        # Best-effort: a failed status edit (e.g. the placeholder
                        # was deleted) must not abort an otherwise healthy run.
                        try:
                            await asyncio.to_thread(_update, channel, status_ts, _escape(status))
                        except Exception:
                            status_ok = False
                            log.exception("status edit failed channel=%s", channel)
                        last_edit = now
                elif isinstance(message, ResultMessage):
                    final = message.result or final
                    cost = getattr(message, "total_cost_usd", None)
                    subtype = getattr(message, "subtype", None)
                    hit_turn_limit = subtype == "error_max_turns"
                    hit_budget_limit = subtype == "error_max_budget_usd"
        except Exception:
            # On the pinned SDK an is_error result (budget/turn limit) is yielded
            # as a ResultMessage and *then* raised out of the stream. We already
            # captured everything from it, so swallow that trailing raise and let
            # the canned limit messages render; propagate anything else.
            if not (hit_budget_limit or hit_turn_limit):
                raise
            log.exception("stream raised after limit result event_id=%s channel=%s", event_id, channel)

    try:
        await asyncio.wait_for(run(), _ANSWER_DEADLINE)
    except Exception as e:
        # One handler for the deadline and agent errors alike: bail with a notice
        # if nothing was salvageable, else promote the last streamed text to the
        # final answer with a note. Budget/turn limits never reach here -- run()
        # swallows their trailing raise so the canned messages below render.
        timed_out = isinstance(e, asyncio.TimeoutError)
        if timed_out:
            _log("timeout", event_id=event_id, channel=channel, thread=thread, user=user)
            icon, gist = ":hourglass:", "I ran out of time"
        else:
            log.exception("answer failed event_id=%s channel=%s thread=%s", event_id, channel, thread)
            icon, gist = ":warning:", "I hit an error"
        if not final and not last_text:
            try:
                _update(channel, status_ts, f"{icon} {gist} and gave up on this one.")
            except Exception:
                log.exception("error-notice update failed event_id=%s channel=%s", event_id, channel)
            return
        if not final:
            final = last_text
            partial_note = f"\n\n_{icon} {gist}; this is what I had so far._"

    if hit_turn_limit and not final:
        final = "I hit my step limit before finishing. Try narrowing the question."
    if hit_budget_limit and not final:
        final = f"I hit my ${budget:.2f} cost limit before finishing. Try narrowing the question."
    body = final or last_text or "_(no answer)_"
    if partial_note:
        body = f"{body}{partial_note}"
    if cost is not None:
        body = f"{body}\n\n_cost: ${cost:.4f}_"
    _log("result", event_id=event_id, channel=channel, thread=thread, user=user, cost=cost, turn_limit=hit_turn_limit, budget_limit=hit_budget_limit)
    if not _deliver(channel, status_ts, thread, body):
        # The answer never reached the user (both first-chunk paths failed), so
        # replace the placeholder with an error notice and release the dedup
        # claim so a redelivery isn't dropped. Guard the notice so the fallback
        # can never itself raise out of the background worker.
        try:
            _update(channel, status_ts, ":warning: I computed an answer but couldn't post it. Please ask again.")
        except Exception:
            log.exception("delivery-failure fallback failed event_id=%s channel=%s", event_id, channel)
        _release_event(event_id)


_WATCH_INTERVAL_SECONDS = 300
# Dispatcher beats ~180s, reconciler ~240s; 900s ≈ 4 missed beats.
_HEARTBEAT_STALE_SECONDS = 900

_EXPERIMENT_DONE_SQL = """
SELECT experiment_id, name, total_trials, pass_count, pass_rate, cost_usd
FROM v_experiment_summary
WHERE NOT is_collection
  AND total_trials > 0
  AND active_trials = 0
  AND last_finished_at > NOW() - INTERVAL '2 hours'
"""


async def _check_experiments_finished() -> list[tuple[str, str]]:
    from urllib.parse import quote

    from tools import _fetch

    dash = _dashboard_url()
    alerts = []
    for r in await _fetch(_EXPERIMENT_DONE_SQL):
        url = f"{dash}/experiments/{quote(quote(r['experiment_id'], safe=''), safe='')}"
        alerts.append((
            f"exp_done:{r['experiment_id']}",
            f":checkered_flag: *{_escape(r['name'] or r['experiment_id'])}* finished — "
            f"{r['pass_count']}/{r['total_trials']} passed ({float(r['pass_rate'] or 0):.0f}%), "
            f"${float(r['cost_usd'] or 0):,.2f} · {url}",
        ))
    return alerts


async def _check_heartbeats() -> list[tuple[str, str]]:
    from tools import _get

    data = await _get("/admin/queue-health")
    alerts = []
    for comp in ("dispatcher", "reconciler"):
        age = (data.get(comp) or {}).get("age_seconds")
        key = f"hb_stale:{comp}"
        if age is None or age > _HEARTBEAT_STALE_SECONDS:
            gist = "has no recorded heartbeat" if age is None else f"last beat {age:.0f}s ago"
            alerts.append((key, f":rotating_light: *{comp}* looks down — {gist}."))
        else:
            # Recovered: re-arm so the next stale episode alerts again.
            try:
                watch_state.pop(key, None)
            except Exception:
                log.exception("failed to re-arm heartbeat alert key=%s", key)
    return alerts


CHECKS = [
    ("experiment_finished", _check_experiments_finished),
    ("heartbeat_stale", _check_heartbeats),
]


@app.function(
    image=image,
    secrets=[secret],
    timeout=120,
    max_containers=1,
    schedule=modal.Period(seconds=_WATCH_INTERVAL_SECONDS),
)
async def watch() -> None:
    """Deterministic checks -> templated Slack alerts. No agent, no log text.

    Alert-once dedupe lives in ``watch_state``; post before claiming so a
    failed post repeats next tick rather than vanishing (max_containers=1,
    so no concurrent duplicate risk).
    """
    channel = os.environ.get("SLACK_ALERT_CHANNEL", "").strip()
    if not channel:
        _log("watch_skipped", reason="SLACK_ALERT_CHANNEL unset")
        return
    for name, check in CHECKS:
        try:
            alerts = await check()
        except Exception:
            log.exception("watch check failed check=%s", name)
            continue
        for key, text in alerts:
            try:
                if watch_state.get(key) is None:
                    _post(channel, None, text)
                    watch_state.put(key, time.time())
                    _log("watch_alert", check=name, key=key)
            except Exception:
                log.exception("watch alert failed check=%s key=%s", name, key)
