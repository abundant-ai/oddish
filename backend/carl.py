from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

import httpx
import modal

_MAX_SLACK = 3900
_SLACK_RETRIES = 3

log = logging.getLogger("oddish.carl")


def _log(event: str, **fields: Any) -> None:
    log.info(
        "%s %s",
        event,
        " ".join(f"{key}={value}" for key, value in fields.items() if value is not None),
    )


def _strip_mention(text: str, bot_user_id: str | None) -> str:
    if bot_user_id:
        text = re.sub(
            rf"^\s*<@{re.escape(bot_user_id)}(?:\|[^>]*)?>\s*", "", text
        )
    return text.strip()


def _bot_user_id(payload: dict, event: dict) -> str | None:
    authorizations = payload.get("authorizations") or []
    for auth in authorizations:
        if auth.get("is_bot") and auth.get("user_id"):
            return auth["user_id"]
    for auth in authorizations:
        if auth.get("user_id"):
            return auth["user_id"]
    match = re.match(r"\s*<@([A-Z0-9]+)(?:\|[^>]*)?>", event.get("text", ""))
    return match.group(1) if match else None


def _seen_events() -> modal.Dict:
    return modal.Dict.from_name("oddish-carl-seen-events", create_if_missing=True)


def _claim_event(event_id: str | None) -> bool:
    if not event_id:
        return True
    return _seen_events().put(event_id, time.time(), skip_if_exists=True)


def _release_event(event_id: str | None) -> None:
    if not event_id:
        return
    try:
        _seen_events().pop(event_id, None)
    except Exception:
        log.exception("failed to release event claim event_id=%s", event_id)


def _bot_token() -> str:
    for name in (
        "SLACK_BOT_TOKEN",
        "ODDISH_SLACK_UNFURL_BOT_TOKEN",
        "SLACK_ALERT_BOT_TOKEN",
    ):
        if token := os.environ.get(name, "").strip():
            return token
    raise RuntimeError("Carl's Slack bot token is not configured")


def _slack_call(method: str, **payload: Any) -> dict:
    headers = {"Authorization": f"Bearer {_bot_token()}"}
    for attempt in range(_SLACK_RETRIES + 1):
        response = httpx.post(
            f"https://slack.com/api/{method}",
            headers=headers,
            json=payload,
            timeout=10,
        )
        if response.status_code == 429 and attempt < _SLACK_RETRIES:
            time.sleep(min(float(response.headers.get("retry-after", "1")), 30))
            continue
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack {method} failed: {data.get('error', 'unknown')}")
        return data


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _split_at(text: str, limit: int) -> int:
    if len(text) <= limit:
        return len(text)
    cut = limit
    amp = text.rfind("&", cut - 4, cut)
    if amp != -1 and ";" not in text[amp:cut]:
        cut = amp
    return cut


def _post(channel: str, thread: str, text: str) -> str:
    cut = _split_at(text, _MAX_SLACK)
    return _slack_call(
        "chat.postMessage", channel=channel, thread_ts=thread, text=text[:cut]
    )["ts"]


def _update(channel: str, ts: str, text: str) -> None:
    cut = _split_at(text, _MAX_SLACK)
    _slack_call("chat.update", channel=channel, ts=ts, text=text[:cut])


def _notify(channel: str, thread: str, text: str, event_id: str | None) -> None:
    try:
        _post(channel, thread, text)
    except Exception:
        log.exception("failed to post reply channel=%s", channel)
        _release_event(event_id)


def _deliver(channel: str, ts: str, thread: str, text: str) -> bool:
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
            return False
        rest = rest[cut:]
    return True


def _spawn_answer(
    channel: str,
    thread: str,
    prompt: str,
    user: str | None,
    event_id: str | None,
) -> None:
    modal.Function.from_name(
        os.environ.get("MODAL_APP_NAME", "oddish"),
        "carl_answer",
        environment_name=os.environ.get("MODAL_ENVIRONMENT") or None,
    ).spawn(channel, thread, prompt, user, event_id)


def dispatch_app_mention(payload: dict) -> None:
    event_id = payload.get("event_id")
    channel: str | None = None
    thread: str | None = None
    claimed = False
    try:
        event = payload.get("event") or {}
        if event.get("type") != "app_mention" or event.get("bot_id"):
            return
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
        if not _claim_event(event_id):
            _log("duplicate", event_id=event_id)
            return
        claimed = True

        channels = os.environ.get("ODDISH_CARL_ALLOWED_CHANNELS", "").strip()
        if channels and channel not in {
            value.strip() for value in channels.split(",") if value.strip()
        }:
            _log("channel_not_allowed", event_id=event_id, channel=channel, user=user)
            _notify(channel, thread, "Carl isn't enabled in this channel.", event_id)
            return

        allowed = os.environ.get("ODDISH_CARL_ALLOWED_USERS", "").strip()
        if not allowed:
            _log("allowlist_unset", event_id=event_id, user=user)
            _notify(
                channel,
                thread,
                "Carl's question allowlist isn't configured; refusing to run.",
                event_id,
            )
            return
        if not user or user not in {
            value.strip() for value in allowed.split(",") if value.strip()
        }:
            _log("unauthorized", event_id=event_id, user=user)
            _notify(channel, thread, "Sorry, you aren't authorized to ask Carl.", event_id)
            return

        prompt = _strip_mention(event.get("text", ""), _bot_user_id(payload, event))
        if not prompt:
            _log("empty_prompt", event_id=event_id, user=user)
            _notify(
                channel,
                thread,
                "Ask a question after the mention, e.g. `@carl why did trial abc123 fail?`",
                event_id,
            )
            return
        _spawn_answer(channel, thread, prompt, user, event_id)
        claimed = False
        _log("claimed", event_id=event_id, channel=channel, thread=thread, user=user)
    except Exception:
        log.exception("dispatch failed event_id=%s", event_id)
        if claimed:
            _release_event(event_id)
        if channel and thread:
            try:
                _post(
                    channel,
                    thread,
                    ":warning: I hit an error handling that mention. Please try again.",
                )
            except Exception:
                log.exception("failed to post dispatch error event_id=%s", event_id)
