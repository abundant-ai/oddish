from __future__ import annotations

from typing import Any

from oddish.observability import span
from oddish.timing import (
    begin_request_timing,
    elapsed_ms,
    finish_handler_timing,
    format_server_timing,
    join_server_timing_headers,
    request_phase_metrics,
    reset_request_timing,
)
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BackendPhaseMetricsMiddleware:
    """Emit wire-safe request phase timings and trace observations."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        timing, timing_token, stage_token = begin_request_timing()
        state = scope.setdefault("state", {})
        state["server_timing_metrics"] = []
        response_started = False
        observation_emitted = False

        def emit_observation(
            *, backend_complete_ms: float, response_complete: bool
        ) -> None:
            nonlocal observation_emitted
            if observation_emitted:
                return
            observation_emitted = True

            if not response_started:
                finish_handler_timing(timing)
                timing.record("backend_total", backend_complete_ms)
            attributes: dict[str, Any] = {
                f"{name}.duration_ms": duration
                for name, duration, _ in request_phase_metrics(timing)
            }
            attributes.update(
                {
                    "backend_complete.duration_ms": backend_complete_ms,
                    "db.query_count": timing.query_count,
                    "handler.db.query_count": timing.handler_query_count,
                    "http.response.body.size": timing.response_bytes,
                    "http.response.complete": response_complete,
                    "http.response.status_code": timing.status_code or 500,
                }
            )
            if timing.cache_hit is not None:
                attributes["auth.cache.hit"] = timing.cache_hit
            with span("backend.request.phases", **attributes):
                pass

        async def send_with_metrics(message: Message) -> None:
            nonlocal response_started
            response_complete = False
            if message["type"] == "http.response.start":
                response_started = True
                timing.status_code = message["status"]
                finish_handler_timing(timing)
                timing.record("backend_total", elapsed_ms(timing.started_at))
                generated = format_server_timing(
                    [*state["server_timing_metrics"], *request_phase_metrics(timing)]
                )
                headers = MutableHeaders(scope=message)
                combined = join_server_timing_headers(
                    headers.get("Server-Timing"), generated
                )
                if combined:
                    headers["Server-Timing"] = combined
            elif message["type"] == "http.response.body":
                timing.response_bytes += len(message.get("body", b""))
                response_complete = not message.get("more_body", False)
            elif message["type"] == "http.response.pathsend":
                response_complete = True
            backend_complete_ms = (
                elapsed_ms(timing.started_at) if response_complete else None
            )
            await send(message)
            if backend_complete_ms is not None:
                emit_observation(
                    backend_complete_ms=backend_complete_ms,
                    response_complete=True,
                )

        try:
            await self.app(scope, receive, send_with_metrics)
        finally:
            emit_observation(
                backend_complete_ms=elapsed_ms(timing.started_at),
                response_complete=False,
            )
            reset_request_timing((timing_token, stage_token))
