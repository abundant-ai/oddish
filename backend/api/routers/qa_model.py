"""Authenticated Anthropic Messages gateway for platform-funded QA attempts.

Raw provider keys remain in the API process. A gateway key authorizes exactly
one live worker attempt; ordinary API keys and analysis READ keys cannot call it.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import os
from urllib.parse import quote

import httpx
import anyio
from botocore.eventstream import EventStreamBuffer
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from oddish.config import to_anthropic_api_model_id, to_bedrock_model_id
from oddish.db import get_session
from oddish.db.models import TrialModel, WorkerJobModel, WorkerJobStatus, utcnow
from oddish.timing import RequestTimedAsyncClient
from oddish.workers.queue.model_capacity import (
    CapacityUnavailable,
    REQUEST_TIMEOUT_SECONDS,
    ProviderPool,
    ROUTE_THRESHOLD,
    configured_pools,
    observe_provider,
    reserve_request,
    settle_request,
)
from oddish.workers.queue.model_gateway import GATEWAY_MODEL, is_gateway_trial

router = APIRouter(prefix="/qa-model", tags=["QA model gateway"])
logger = logging.getLogger(__name__)
MAX_BODY_BYTES = 8 * 1024 * 1024


async def authorize_worker(request: Request) -> str:
    # Disabling new routing must not interrupt already-issued attempt tokens.
    credential = request.headers.get("x-api-key") or request.headers.get(
        "authorization", ""
    ).removeprefix("Bearer ")
    job_id, sep, token = credential.partition(".")
    if not sep or not token:
        raise HTTPException(401, "Invalid QA model credential")
    async with get_session() as session:
        job = await session.get(WorkerJobModel, job_id)
        if (
            job is None
            or not job.job_token_hash
            or not hmac.compare_digest(
                job.job_token_hash, hashlib.sha256(token.encode()).hexdigest()
            )
            or job.job_token_revoked_at is not None
            or job.job_token_expires_at is None
            or job.job_token_expires_at <= utcnow()
            or job.status != WorkerJobStatus.RUNNING
            or job.subject_table != "trials"
        ):
            raise HTTPException(401, "Inactive QA model credential")
        trial = await session.get(TrialModel, job.subject_id)
        if (
            trial is None
            or trial.org_id != job.org_id
            or not is_gateway_trial(
                kind=trial.kind,
                agent=trial.agent,
                model=trial.model,
                byok_env=None,
                harbor_config=trial.harbor_config or {},
            )
        ):
            raise HTTPException(403, "Credential is not bound to a supported QA trial")
    return job_id


def upstream_request(
    pool: ProviderPool, payload: dict, betas: list[str], *, count: bool
) -> tuple[str, dict, dict]:
    model = (
        to_anthropic_api_model_id(pool.model)
        if pool.provider == "anthropic"
        else to_bedrock_model_id(pool.model)
    )
    body = {**payload, "model": model}
    if pool.provider == "anthropic":
        headers = {
            "x-api-key": os.environ[pool.key_env],
            "anthropic-version": "2023-06-01",
        }
        if betas:
            headers["anthropic-beta"] = ",".join(betas)
        path = "messages/count_tokens" if count else "messages"
        return f"https://api.anthropic.com/v1/{path}", headers, body
    # Bedrock's bearer-token route uses the stored account credential; there is
    # no global CLAUDE_CODE_USE_BEDROCK change and no direct-API fallback here.
    body.pop("model")
    streaming = body.pop("stream", False)
    body["anthropic_version"] = "bedrock-2023-05-31"
    if betas:
        body["anthropic_beta"] = betas
    path = "invoke-with-response-stream" if streaming else "invoke"
    url = f"https://bedrock-runtime.{pool.region}.amazonaws.com/model/{quote(model, safe='')}/{path}"
    return url, {"Authorization": f"Bearer {os.environ[pool.key_env]}"}, body


async def message_events(response: httpx.Response, provider: str):
    if provider == "bedrock":
        buffer = EventStreamBuffer()
        async for chunk in response.aiter_bytes():
            buffer.add_data(chunk)
            for event in buffer:
                if event.headers.get(":event-type") != "chunk":
                    raise RuntimeError("Bedrock stream failed")
                envelope = json.loads(event.payload)
                yield json.loads(base64.b64decode(envelope["bytes"]))
    else:
        data = []
        async for line in response.aiter_lines():
            if not line:
                if data:
                    yield json.loads("\n".join(data))
                    data = []
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
        if data:
            yield json.loads("\n".join(data))


def retry_seconds(headers: httpx.Headers) -> int:
    try:
        return max(1, math.ceil(float(headers.get("retry-after", "30"))))
    except (ValueError, OverflowError):
        return 30


async def proxy_message(request: Request, *, count: bool = False):
    job_id = await authorize_worker(request)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY_BYTES:
            raise HTTPException(413, "QA model request is too large")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Invalid JSON") from None
    if not isinstance(payload, dict) or payload.get("model") != GATEWAY_MODEL:
        raise HTTPException(422, "This gateway serves QA Sonnet 5 only")
    if not isinstance(payload.get("messages"), list):
        raise HTTPException(422, "messages must be a list")
    if type(payload.get("stream", False)) is not bool:
        raise HTTPException(422, "stream must be a boolean")
    output_tokens = payload.get("max_tokens", 0)
    if not count and (type(output_tokens) is not int or output_tokens <= 0):
        raise HTTPException(422, "max_tokens must be a positive integer")

    # Conservative text/tool prompt estimate. Cache hits are not assumed across
    # providers; actual input + cache-write usage replaces this after completion.
    # The JSON byte count intentionally overestimates text tokenization.
    # URL/image/document/audio content has provider-specific token costs that
    # cannot be bounded by JSON bytes. Reject it until a token-count adapter is
    # available; silently under-reserving would violate the shared budget.
    def check_content(value):
        if isinstance(value, dict):
            if value.get("type") in {
                "image",
                "document",
                "audio",
                "image_url",
                "input_audio",
            }:
                raise HTTPException(
                    422, "QA gateway currently supports text and tool content only"
                )
            for child in value.values():
                check_content(child)
        elif isinstance(value, list):
            for child in value:
                check_content(child)

    check_content(payload)
    input_tokens = 0 if count else len(raw)
    output_tokens = 0 if count else output_tokens
    betas = [
        b.strip()
        for b in request.headers.get("anthropic-beta", "").split(",")
        if b.strip()
    ]
    pools = [
        p
        for p in configured_pools()
        if (not count or p.provider == "anthropic")
        and (p.provider == "anthropic" or set(betas).issubset(p.supported_betas))
    ]
    if not pools:
        raise HTTPException(503, "No compatible QA model pool is configured")
    if all(
        p.external_load_fraction + p.load(1, input_tokens, output_tokens)
        > ROUTE_THRESHOLD
        for p in pools
    ):
        raise HTTPException(413, "Request exceeds every configured QA routing budget")
    try:
        reservation = await reserve_request(
            pools,
            worker_job_id=job_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    except CapacityUnavailable:
        return JSONResponse(
            status_code=429,
            headers={"retry-after": "5"},
            content={
                "type": "error",
                "error": {
                    "type": "rate_limit_error",
                    "message": "QA model pools are busy; retry shortly",
                },
            },
        )
    logger.info(
        "qa_model_admitted pool=%s worker_job=%s reserved_input=%s reserved_output=%s",
        reservation.pool.id,
        job_id,
        input_tokens,
        output_tokens,
    )
    client = RequestTimedAsyncClient(
        timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=15)
    )
    response = None
    headers = {}
    cleaned = False

    async def cleanup(usage=None):
        nonlocal cleaned
        if cleaned:
            return
        with anyio.CancelScope(shield=True):
            try:
                if response is not None:
                    await response.aclose()
            finally:
                try:
                    await client.aclose()
                finally:
                    await settle_request(reservation, usage=usage)
                    cleaned = True

    deadline = asyncio.get_running_loop().time() + REQUEST_TIMEOUT_SECONDS
    try:
        url, upstream_headers, body = upstream_request(
            reservation.pool, payload, betas, count=count
        )
        async with asyncio.timeout_at(deadline):
            response = await client.send(
                client.build_request("POST", url, headers=upstream_headers, json=body),
                stream=True,
            )
        headers = dict(response.headers)
        status = response.status_code
        cooldown = (
            retry_seconds(response.headers)
            if status == 429 or status >= 500
            else 300
            if status in {401, 403, 404}
            else 0
        )
        await observe_provider(
            reservation.pool.quota_group, headers, cooldown_seconds=cooldown
        )
        if response.status_code != 200:
            await cleanup(
                {"input_tokens": 0, "output_tokens": 0} if status < 500 else None
            )
            return JSONResponse(
                status_code=503 if status in {401, 403, 404} else status,
                headers={"retry-after": str(min(cooldown, 5))} if cooldown else {},
                content={
                    "type": "error",
                    "error": {
                        "type": "rate_limit_error" if status == 429 else "api_error",
                        "message": "QA model provider rejected the request",
                    },
                },
            )
        if count or not payload.get("stream", False):
            async with asyncio.timeout_at(deadline):
                result = json.loads(await response.aread())
            usage = (
                {"input_tokens": 0, "output_tokens": 0}
                if count
                else result.get("usage")
            )
            await cleanup(usage)
            return JSONResponse(result)
    except BaseException as exc:
        try:
            if isinstance(exc, (httpx.HTTPError, TimeoutError)):
                with anyio.CancelScope(shield=True):
                    await observe_provider(
                        reservation.pool.quota_group, {}, cooldown_seconds=30
                    )
        finally:
            await cleanup()
        raise

    async def stream():
        usage = {}
        complete = False
        try:
            async with asyncio.timeout_at(deadline):
                async for event in message_events(response, reservation.pool.provider):
                    if event.get("type") == "message_start":
                        usage.update(event.get("message", {}).get("usage", {}))
                    elif event.get("type") == "message_delta":
                        usage.update(event.get("usage", {}))
                    elif event.get("type") == "message_stop":
                        complete = True
                    elif event.get("type") == "error":
                        # Persist before yielding: the client can disconnect as
                        # soon as it sees the error and retry on another replica.
                        with anyio.CancelScope(shield=True):
                            await observe_provider(
                                reservation.pool.quota_group, {}, cooldown_seconds=30
                            )
                    yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
                    if event.get("type") == "error":
                        return
                if not complete:
                    raise RuntimeError("QA model stream ended without message_stop")
        except (httpx.HTTPError, TimeoutError, RuntimeError, ValueError):
            with anyio.CancelScope(shield=True):
                await observe_provider(
                    reservation.pool.quota_group, {}, cooldown_seconds=30
                )
            logger.warning(
                "qa_model_stream_failed pool=%s worker_job=%s",
                reservation.pool.id,
                job_id,
            )
            yield b'event: error\ndata: {"type":"error","error":{"type":"api_error","message":"QA model stream interrupted"}}\n\n'
        finally:
            await cleanup(usage if complete else None)
            logger.info(
                "qa_model_request pool=%s worker_job=%s complete=%s",
                reservation.pool.id,
                job_id,
                complete,
            )

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        background=BackgroundTask(cleanup),
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )


@router.post("/v1/messages")
async def messages(request: Request):
    return await proxy_message(request)


@router.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    return await proxy_message(request, count=True)
