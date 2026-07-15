"""Slack Events API endpoint for Oddish link unfurls."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from api.services.slack_unfurls import (
    load_slack_unfurl_config,
    process_link_shared_event,
)

router = APIRouter(prefix="/webhooks/slack", tags=["Slack"])


def verify_slack_signature(
    body: bytes,
    *,
    timestamp: str | None,
    signature: str | None,
    signing_secret: str,
    now: int | None = None,
) -> bool:
    if not timestamp or not signature or not signing_secret:
        return False
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    current = int(time.time()) if now is None else now
    if abs(current - sent_at) > 60 * 5:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = (
        "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(expected, signature)


@router.post("/events")
async def slack_events(request: Request, background_tasks: BackgroundTasks) -> dict:
    config = load_slack_unfurl_config()
    if not config.ready:
        raise HTTPException(status_code=503, detail="Slack unfurls are not configured")

    body = await request.body()
    if not verify_slack_signature(
        body,
        timestamp=request.headers.get("X-Slack-Request-Timestamp"),
        signature=request.headers.get("X-Slack-Signature"),
        signing_secret=config.signing_secret,
    ):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid Slack payload") from exc

    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}
    if payload.get("type") != "event_callback":
        return {"ok": True}
    if config.team_id and payload.get("team_id") != config.team_id:
        return {"ok": True}

    event = payload.get("event") or {}
    if event.get("type") != "link_shared":
        return {"ok": True}
    if config.allowed_channels and event.get("channel") not in config.allowed_channels:
        return {"ok": True}

    background_tasks.add_task(process_link_shared_event, payload)
    return {"ok": True}
