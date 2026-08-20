from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, text

from api import request_metrics
from oddish.timing import (
    REQUEST_PHASES,
    RequestTimedAsyncClient,
    begin_auth_timing,
    begin_request_timing,
    current_request_timing,
    finish_auth_timing,
    record_cache_result,
    reset_request_timing,
    timed_phase,
)


async def _run_request(*, cold: bool, body: bytes = b'{"ok":true}'):
    sent = []

    async def app(scope, receive, send):
        auth = begin_auth_timing()
        with timed_phase("auth_verify", credential_kind="test"):
            await asyncio.sleep(0)
        with timed_phase("auth_cache", credential_kind="test"):
            record_cache_result(hit=not cold)
        timing = current_request_timing()
        assert timing is not None
        if cold:
            with timed_phase("db_checkout"):
                await asyncio.sleep(0)
            timing.record("db_sql", 1.25, stage="auth")

            async def external_response(_request):
                await asyncio.sleep(0.01)
                return httpx.Response(200)

            async with RequestTimedAsyncClient(
                transport=httpx.MockTransport(external_response)
            ) as client:
                await client.get("https://example.test")
            with timed_phase("db_commit"):
                await asyncio.sleep(0)
        finish_auth_timing(auth)
        timing.record("db_sql", 0.5, stage="handler")
        scope["state"]["server_timing_metrics"].append(
            ("route_build", 2.0, "Route build")
        )
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"server-timing", b"upstream;dur=1.0")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    middleware = request_metrics.BackendPhaseMetricsMiddleware(app)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await middleware(
        {"type": "http", "method": "GET", "path": "/", "headers": []},
        receive,
        send,
    )
    return sent


@pytest.mark.asyncio
async def test_cold_and_warm_requests_emit_every_backend_phase(monkeypatch):
    observations = []

    @contextmanager
    def capture_span(name, **attributes):
        if name == "backend.request.phases":
            observations.append(attributes)
        yield

    monkeypatch.setattr(request_metrics, "span", capture_span)

    for cold in (True, False):
        messages = await _run_request(cold=cold)
        headers = dict(messages[0]["headers"])
        server_timing = headers[b"server-timing"].decode()
        assert "upstream;dur=1.0" in server_timing
        assert "route_build;dur=2.0" in server_timing
        assert all(f"{phase};dur=" in server_timing for phase in REQUEST_PHASES)

    assert observations[0]["db.query_count"] == 2
    assert observations[0]["handler.db.query_count"] == 1
    assert observations[0]["auth.cache.hit"] is False
    assert observations[0]["external_http.duration_ms"] >= 5
    assert observations[1]["db.query_count"] == 1
    assert observations[1]["auth.cache.hit"] is True
    assert all(
        f"{phase}.duration_ms" in observation
        for observation in observations
        for phase in REQUEST_PHASES
    )


@pytest.mark.asyncio
async def test_twenty_way_observations_keep_query_counts_and_bytes_isolated(
    monkeypatch,
):
    observations = []

    @contextmanager
    def capture_span(name, **attributes):
        if name == "backend.request.phases":
            observations.append(attributes)
        yield

    monkeypatch.setattr(request_metrics, "span", capture_span)
    bodies = [f'{{"request":{index}}}'.encode() for index in range(20)]
    responses = await asyncio.gather(
        *(
            _run_request(cold=index % 2 == 0, body=body)
            for index, body in enumerate(bodies)
        )
    )

    assert len(responses) == len(observations) == 20
    assert sorted(item["http.response.body.size"] for item in observations) == sorted(
        map(len, bodies)
    )
    assert (
        sorted(item["db.query_count"] for item in observations) == [1] * 10 + [2] * 10
    )
    assert all("payload" not in key for item in observations for key in item)
    assert all(body not in item.values() for item in observations for body in bodies)


@pytest.mark.asyncio
async def test_stream_trace_records_completion_after_final_body(monkeypatch):
    observations = []
    sent = []
    background_started = asyncio.Event()
    finish_background = asyncio.Event()

    @contextmanager
    def capture_span(name, **attributes):
        if name == "backend.request.phases":
            observations.append(attributes)
        yield

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await asyncio.sleep(0.02)
        await send(
            {
                "type": "http.response.body",
                "body": b"streamed",
                "more_body": False,
            }
        )
        background_started.set()
        await finish_background.wait()

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    monkeypatch.setattr(request_metrics, "span", capture_span)
    request = asyncio.create_task(
        request_metrics.BackendPhaseMetricsMiddleware(app)(
            {"type": "http", "method": "GET", "path": "/stream", "headers": []},
            receive,
            send,
        )
    )
    await background_started.wait()
    observed_before_background_finished = len(observations) == 1
    finish_background.set()
    await request

    assert observed_before_background_finished
    assert len(observations) == 1
    observation = observations[0]
    assert observation["backend_complete.duration_ms"] >= 15
    assert (
        observation["backend_complete.duration_ms"]
        > observation["backend_total.duration_ms"]
    )
    assert observation["http.response.body.size"] == len(b"streamed")
    assert observation["http.response.complete"] is True
    server_timing = dict(sent[0]["headers"])[b"server-timing"].decode()
    assert "backend_total;dur=" in server_timing
    assert "backend_complete" not in server_timing


@pytest.mark.asyncio
async def test_outer_metrics_cover_unhandled_errors_and_post_handler_work():
    api = FastAPI()

    @api.middleware("http")
    async def record_post_handler_work(_request, call_next):
        response = await call_next(_request)
        timing = current_request_timing()
        assert timing is not None
        timing.record("db_sql", 12.0, stage="handler")
        return response

    @api.get("/ok")
    async def ok():
        return {"ok": True}

    @api.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    transport = httpx.ASGITransport(
        app=request_metrics.BackendPhaseMetricsMiddleware(api),
        raise_app_exceptions=False,
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        success = await client.get("/ok")
        failure = await client.get("/boom")

    assert "db_sql;dur=12.0" in success.headers["server-timing"]
    assert failure.status_code == 500
    assert "backend_total;dur=" in failure.headers["server-timing"]


def test_sql_listener_counts_successful_and_failed_statements():
    from oddish.db.connection import _install_request_query_timing

    engine = create_engine("sqlite://")
    _install_request_query_timing(SimpleNamespace(sync_engine=engine))
    timing, timing_token, stage_token = begin_request_timing()
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            with pytest.raises(Exception):
                connection.execute(text("SELECT missing_column"))
        assert timing.query_count == 2
        assert timing.handler_query_count == 2
        assert timing.durations_ms["db_sql"] >= 0
    finally:
        reset_request_timing((timing_token, stage_token))
        engine.dispose()
