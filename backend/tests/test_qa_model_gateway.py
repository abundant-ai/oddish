"""Wire-protocol tests; no provider credentials or paid model calls."""

import base64
import hashlib
import json
import struct
import zlib
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from api.routers import qa_model as gateway
from oddish.db.models import WorkerJobModel, WorkerJobStatus, utcnow
from oddish.workers.queue.model_capacity import (
    ProviderPool,
    Reservation,
    CapacityUnavailable,
)


def pool(provider="anthropic"):
    common = dict(
        id=provider,
        quota_group=provider,
        provider=provider,
        key_env="QA_TEST_KEY",
        model="claude-sonnet-5",
        requests_per_minute=100000,
    )
    if provider == "bedrock":
        return ProviderPool(
            **{**common, "model": "global.anthropic.claude-sonnet-5"},
            region="us-east-2",
            tokens_per_minute=10000000,
            output_multiplier=10,
        )
    return ProviderPool(
        **common, input_tokens_per_minute=10000000, output_tokens_per_minute=10000000
    )


@pytest.fixture
def harness(monkeypatch):
    sent = []
    reserved = []
    settled = []
    observed = []
    monkeypatch.setenv("QA_TEST_KEY", "provider-secret")

    async def authorize(request):
        return "worker-1"

    async def reserve(pools, **kw):
        reserved.append(kw)
        return Reservation("lease-1", pools[0])

    async def settle(reservation, **kw):
        settled.append(kw)

    async def observe(pool_id, headers, **kw):
        observed.append((pool_id, headers, kw))

    monkeypatch.setattr(gateway, "authorize_worker", authorize)
    monkeypatch.setattr(gateway, "reserve_request", reserve)
    monkeypatch.setattr(gateway, "settle_request", settle)
    monkeypatch.setattr(gateway, "observe_provider", observe)
    monkeypatch.setattr(gateway, "configured_pools", lambda: [pool()])

    def transport(handler):
        async def send(request):
            sent.append(request)
            return handler(request)

        monkeypatch.setattr(
            gateway,
            "RequestTimedAsyncClient",
            lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(send), **kw),
        )

    app = FastAPI()
    app.include_router(gateway.router)
    return SimpleNamespace(
        app=app,
        transport=transport,
        sent=sent,
        reserved=reserved,
        settled=settled,
        observed=observed,
    )


def payload(**extra):
    return {
        "model": "claude-sonnet-5",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "check the logs"}],
        **extra,
    }


@pytest.mark.asyncio
async def test_json_request_is_forwarded_and_actual_usage_settled(harness):
    result = {
        "id": "msg_1",
        "type": "message",
        "content": [],
        "usage": {
            "input_tokens": 12,
            "cache_creation_input_tokens": 3,
            "output_tokens": 4,
        },
    }
    harness.transport(lambda _: httpx.Response(200, json=result))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app), base_url="http://test"
    ) as client:
        r = await client.post("/qa-model/v1/messages", json=payload())
    assert r.json() == result
    assert harness.sent[0].headers["x-api-key"] == "provider-secret"
    assert str(harness.sent[0].url) == "https://api.anthropic.com/v1/messages"
    assert harness.settled == [{"usage": result["usage"]}]
    assert harness.reserved[0]["output_tokens"] == 100


@pytest.mark.asyncio
async def test_token_count_endpoint_does_not_charge_prompt_tokens(harness):
    harness.transport(lambda _: httpx.Response(200, json={"input_tokens": 8}))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app), base_url="http://test"
    ) as client:
        r = await client.post("/qa-model/v1/messages/count_tokens", json=payload())
    assert r.status_code == 200
    assert (
        harness.reserved[0]["input_tokens"] == harness.reserved[0]["output_tokens"] == 0
    )
    assert harness.sent[0].url.path == "/v1/messages/count_tokens"


EVENTS = [
    {
        "type": "message_start",
        "message": {
            "id": "msg_1",
            "usage": {
                "input_tokens": 8,
                "cache_creation_input_tokens": 2,
                "cache_read_input_tokens": 1000,
            },
        },
    },
    {
        "type": "content_block_start",
        "index": 0,
        "content_block": {
            "type": "tool_use",
            "id": "tool_1",
            "name": "Read",
            "input": {},
        },
    },
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": '{"path":"a"}'},
    },
    {"type": "content_block_stop", "index": 0},
    {
        "type": "message_delta",
        "delta": {"stop_reason": "tool_use"},
        "usage": {"output_tokens": 20},
    },
    {"type": "message_stop"},
]


class Chunks(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        for i in range(0, len(self.data), 7):
            yield self.data[i : i + 7]


def bedrock_frame(event):
    name = b":event-type"
    value = b"chunk"
    headers = (
        bytes([len(name)]) + name + b"\x07" + struct.pack(">H", len(value)) + value
    )
    data = json.dumps(
        {"bytes": base64.b64encode(json.dumps(event).encode()).decode()}
    ).encode()
    prelude = struct.pack(">II", 16 + len(headers) + len(data), len(headers))
    frame = prelude + struct.pack(">I", zlib.crc32(prelude)) + headers + data
    return frame + struct.pack(">I", zlib.crc32(frame))


@pytest.mark.parametrize("provider", ["anthropic", "bedrock"])
@pytest.mark.asyncio
async def test_stream_preserves_tool_events_and_cache_accounting(
    harness, monkeypatch, provider
):
    monkeypatch.setattr(gateway, "configured_pools", lambda: [pool(provider)])
    if provider == "bedrock":
        data = b"".join(bedrock_frame(e) for e in EVENTS)
    else:
        data = b"".join(
            f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in EVENTS
        )
    harness.transport(lambda _: httpx.Response(200, stream=Chunks(data)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app), base_url="http://test"
    ) as client:
        r = await client.post("/qa-model/v1/messages", json=payload(stream=True))
    actual = [
        json.loads(line[6:])
        for line in r.text.splitlines()
        if line.startswith("data: ")
    ]
    assert actual == EVENTS
    assert harness.settled == [
        {
            "usage": {
                "input_tokens": 8,
                "cache_creation_input_tokens": 2,
                "cache_read_input_tokens": 1000,
                "output_tokens": 20,
            }
        }
    ]
    if provider == "bedrock":
        body = json.loads(harness.sent[0].content)
        assert "model" not in body and "stream" not in body
        assert body["anthropic_version"] == "bedrock-2023-05-31"
        assert harness.sent[0].url.path.endswith("/invoke-with-response-stream")
        assert harness.sent[0].headers["authorization"] == "Bearer provider-secret"


@pytest.mark.asyncio
async def test_busy_pool_returns_retry_without_contacting_provider(
    harness, monkeypatch
):
    async def busy(*args, **kw):
        raise CapacityUnavailable()

    monkeypatch.setattr(gateway, "reserve_request", busy)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app), base_url="http://test"
    ) as client:
        r = await client.post("/qa-model/v1/messages", json=payload())
    assert r.status_code == 429 and r.headers["retry-after"] == "5"
    assert not harness.sent


@pytest.mark.asyncio
async def test_throttling_records_cooldown_and_never_replays_request(harness):
    harness.transport(lambda _: httpx.Response(429, headers={"retry-after": "7"}))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app), base_url="http://test"
    ) as client:
        r = await client.post("/qa-model/v1/messages", json=payload())
    assert r.status_code == 429 and r.headers["retry-after"] == "5"
    assert len(harness.sent) == 1
    assert harness.observed[0][2] == {"cooldown_seconds": 7}
    assert harness.settled == [{"usage": {"input_tokens": 0, "output_tokens": 0}}]


@pytest.mark.asyncio
async def test_truncated_stream_does_not_refund_unobserved_output(harness):
    harness.transport(
        lambda _: httpx.Response(
            200,
            stream=Chunks(
                b'data: {"type":"message_start","message":{"usage":{"input_tokens":8}}}\n\n'
            ),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app), base_url="http://test"
    ) as client:
        r = await client.post("/qa-model/v1/messages", json=payload(stream=True))
    assert "QA model stream interrupted" in r.text
    assert harness.observed[-1][2] == {"cooldown_seconds": 30}
    assert harness.settled == [{"usage": None}]
    assert len(harness.sent) == 1


@pytest.mark.asyncio
async def test_unverified_bedrock_beta_is_not_silently_dropped(harness, monkeypatch):
    monkeypatch.setattr(gateway, "configured_pools", lambda: [pool("bedrock")])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/qa-model/v1/messages",
            json=payload(),
            headers={"anthropic-beta": "unknown-beta"},
        )
    assert r.status_code == 503 and not harness.sent


@pytest.mark.asyncio
async def test_image_cost_is_not_guessed_from_url_length(harness):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/qa-model/v1/messages",
            json=payload(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "url",
                                    "url": "https://example.com/a.png",
                                },
                            }
                        ],
                    }
                ]
            ),
        )
    assert r.status_code == 422 and not harness.sent


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"job_token_revoked_at": utcnow()},
        {"status": WorkerJobStatus.SUCCESS},
        {"job_token_expires_at": utcnow() - timedelta(seconds=1)},
        {"job_token_hash": "incorrect"},
    ],
)
@pytest.mark.asyncio
async def test_gateway_auth_is_bound_to_a_live_worker(monkeypatch, change):
    job = SimpleNamespace(
        job_token_hash=hashlib.sha256(b"secret").hexdigest(),
        job_token_revoked_at=None,
        job_token_expires_at=utcnow() + timedelta(hours=1),
        status=WorkerJobStatus.RUNNING,
        subject_table="trials",
        subject_id="trial",
        org_id="org",
    )
    job.__dict__.update(change)
    trial = SimpleNamespace(
        org_id="org",
        kind="qa",
        agent="claude-code",
        model="global.anthropic.claude-sonnet-5",
        harbor_config={},
    )

    class Session:
        async def get(self, cls, id):
            return job if cls is WorkerJobModel else trial

    @asynccontextmanager
    async def session():
        yield Session()

    monkeypatch.setattr(gateway, "get_session", session)
    request = Request({"type": "http", "headers": [(b"x-api-key", b"worker.secret")]})
    if change:
        with pytest.raises(HTTPException) as exc:
            await gateway.authorize_worker(request)
        assert exc.value.status_code == 401
    else:
        assert await gateway.authorize_worker(request) == "worker"


@pytest.mark.asyncio
async def test_disconnect_closes_upstream_and_keeps_conservative_reservation(
    harness, monkeypatch
):
    import asyncio

    closed = []

    class Hanging(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"message_start","message":{"usage":{"input_tokens":8}}}\n\n'
            await asyncio.Event().wait()

        async def aclose(self):
            closed.append(True)

    harness.transport(lambda _: httpx.Response(200, stream=Hanging()))
    request_body = json.dumps(payload(stream=True)).encode()

    async def receive():
        return {"type": "http.request", "body": request_body}

    request = Request(
        {"type": "http", "method": "POST", "headers": []}, receive=receive
    )
    response = await gateway.proxy_message(request)
    iterator = response.body_iterator
    await anext(iterator)
    pending = asyncio.create_task(anext(iterator))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert closed and harness.settled == [{"usage": None}]
    assert all(kw["cooldown_seconds"] == 0 for _, _, kw in harness.observed)
    await response.background()
    assert len(harness.settled) == 1


@pytest.mark.asyncio
async def test_invalid_provider_credentials_cool_down_pool_without_leaking_body(
    harness,
):
    harness.transport(lambda _: httpx.Response(401, json={"error": "provider-secret"}))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app), base_url="http://test"
    ) as client:
        r = await client.post("/qa-model/v1/messages", json=payload())
    assert r.status_code == 503 and r.headers["retry-after"] == "5"
    assert "provider-secret" not in r.text
    assert harness.observed[0][2] == {"cooldown_seconds": 300}


@pytest.mark.asyncio
async def test_streamed_overload_cools_pool_before_error_is_delivered(harness):
    error = {
        "type": "error",
        "error": {"type": "overloaded_error", "message": "Overloaded"},
    }
    harness.transport(
        lambda _: httpx.Response(
            200, content=f"data: {json.dumps(error)}\n\n".encode()
        )
    )

    async def receive():
        return {"type": "http.request", "body": json.dumps(payload(stream=True)).encode()}

    response = await gateway.proxy_message(
        Request({"type": "http", "method": "POST", "headers": []}, receive=receive)
    )
    iterator = response.body_iterator
    first = await anext(iterator)
    assert json.loads(first.decode().split("data: ")[1]) == error
    assert harness.observed[-1][2] == {"cooldown_seconds": 30}
    await iterator.aclose()
    await response.background()
    assert harness.settled == [{"usage": None}]
    assert len(harness.sent) == 1


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout])
@pytest.mark.asyncio
async def test_transport_failure_cools_pool_and_releases_resources(harness, failure):
    def fail(request):
        raise failure("upstream unavailable", request=request)

    harness.transport(fail)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app), base_url="http://test"
    ) as client:
        with pytest.raises(failure):
            await client.post("/qa-model/v1/messages", json=payload())
    assert harness.observed[-1][2] == {"cooldown_seconds": 30}
    assert harness.settled == [{"usage": None}]
