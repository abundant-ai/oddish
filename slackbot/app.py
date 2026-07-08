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
    )
    .add_local_python_source("tools")
)

app = modal.App("oddish-slackbot")
secret = modal.Secret.from_name(SECRET_NAME)
seen_events = modal.Dict.from_name("oddish-slackbot-seen-events", create_if_missing=True)

SYSTEM_PROMPT = (
    "You answer teammates' questions about the oddish eval platform (spend, "
    "queue health, why a trial failed) using the provided read-only tools. Be concise. "
    "Format for Slack mrkdwn (*bold*, `code`, • bullets; never markdown headings or tables). "
    "Always include concrete numbers. Call tools rather than guessing. Today's date and all "
    "live data come from the tools and environment."
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
            _notify(channel, thread, "This bot's allowlist is not configured; refusing to run.")
            return
        if not user or user not in {u.strip() for u in allowed.split(",") if u.strip()}:
            _log("unauthorized", event_id=event_id, user=user)
            _notify(channel, thread, "Sorry, you're not authorized to use this bot.")
            return

        cleaned = _strip_mention(event.get("text", ""), _bot_user_id(payload, event))
        if not cleaned:
            _log("empty_prompt", event_id=event_id, user=user)
            _notify(channel, thread, "Ask me a question after the mention, e.g. `@Oddish Claude why did trial abc123 fail?`")
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


def _post(channel: str, thread: str, text: str) -> str:
    return _client().chat_postMessage(channel=channel, thread_ts=thread, text=text[:_MAX_SLACK])["ts"]


def _notify(channel: str, thread: str, text: str) -> None:
    """Best-effort terminal reply (rejection / hint).

    Swallows Slack errors so a failed post cannot bubble into the generic
    dispatch error handler and mask the specific message with a vague one.
    """
    try:
        _post(channel, thread, text)
    except Exception:
        log.exception("failed to post reply channel=%s", channel)


def _update(channel: str, ts: str, text: str) -> None:
    _client().chat_update(channel=channel, ts=ts, text=text[:_MAX_SLACK])


def _deliver(channel: str, ts: str, thread: str, text: str) -> None:
    text = _escape(text)
    _update(channel, ts, text[:_MAX_SLACK])
    rest = text[_MAX_SLACK:]
    while rest:
        _post(channel, thread, rest[:_MAX_SLACK])
        rest = rest[_MAX_SLACK:]


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

    from tools import allowed_tool_names, build_server

    _log("spawned", event_id=event_id, channel=channel, thread=thread, user=user)
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
    options = ClaudeAgentOptions(
        model=MODEL,
        mcp_servers={"oddish": build_server()},
        allowed_tools=allowed_tool_names(),
        permission_mode="dontAsk",
        max_turns=25,
        system_prompt=SYSTEM_PROMPT,
    )

    final = ""
    last_text = ""
    last_edit = 0.0
    cost = None
    hit_turn_limit = False
    partial_note = ""

    async def run() -> None:
        nonlocal final, last_text, last_edit, cost, hit_turn_limit
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
                if status and now - last_edit > 2:
                    await asyncio.to_thread(_update, channel, status_ts, _escape(status))
                    last_edit = now
            elif isinstance(message, ResultMessage):
                final = message.result or final
                cost = getattr(message, "total_cost_usd", None)
                hit_turn_limit = getattr(message, "subtype", None) == "error_max_turns"

    try:
        await asyncio.wait_for(run(), _ANSWER_DEADLINE)
    except asyncio.TimeoutError:
        _log("timeout", event_id=event_id, channel=channel, thread=thread, user=user)
        if not final and not last_text:
            _update(channel, status_ts, ":hourglass: I ran out of time on this one before finishing.")
            return
        # A ResultMessage arrived just as we hit the deadline; deliver the
        # ready answer below. If no result landed but the agent already
        # streamed some text, deliver that partial text rather than losing
        # it behind a bare timeout notice.
        if not final:
            final = last_text
            partial_note = "\n\n_:hourglass: I ran out of time; this is what I had so far._"
    except Exception:
        log.exception("answer failed event_id=%s channel=%s thread=%s", event_id, channel, thread)
        if not final and not last_text:
            _update(channel, status_ts, ":warning: I hit an error and gave up on this one.")
            return
        # A ResultMessage already produced a complete answer before the error;
        # deliver it below. If none did but the agent streamed some text,
        # deliver that partial text rather than discarding it.
        if not final:
            final = last_text
            partial_note = "\n\n_:warning: I hit an error; this is what I had so far._"

    if hit_turn_limit and not final:
        final = "I hit my step limit before finishing. Try narrowing the question."
    body = final or "_(no answer)_"
    if partial_note:
        body = f"{body}{partial_note}"
    if cost:
        body = f"{body}\n\n_cost: ${cost:.4f}_"
    _log("result", event_id=event_id, channel=channel, thread=thread, user=user, cost=cost, turn_limit=hit_turn_limit)
    try:
        _deliver(channel, status_ts, thread, body)
    except Exception:
        log.exception("delivery failed event_id=%s channel=%s thread=%s", event_id, channel, thread)
        _update(channel, status_ts, ":warning: I computed an answer but couldn't post it. Please ask again.")
