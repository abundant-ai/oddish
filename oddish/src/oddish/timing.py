from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Callable

TimingMetric = tuple[str, float, str | None]
TimingRecorder = Callable[[str, float, str | None], None]

_PHASE_DESCRIPTIONS = {
    "auth_verify": "Credential verification",
    "auth_cache": "Principal cache lookup",
    "auth_total": "Auth dependency total",
    "db_checkout": "Database pool checkout",
    "db_sql": "SQL execution",
    "external_http": "External HTTP",
    "db_commit": "Database commit",
    "handler_db": "Handler database total",
    "handler_total": "Handler time to response start",
    "backend_total": "Backend time to response start",
}
REQUEST_PHASES = tuple(_PHASE_DESCRIPTIONS)
_DB_PHASES = frozenset({"db_checkout", "db_sql", "db_commit"})


@dataclass
class RequestTiming:
    started_at: float = field(default_factory=perf_counter)
    durations_ms: dict[str, float] = field(default_factory=dict)
    query_count: int = 0
    handler_query_count: int = 0
    response_bytes: int = 0
    status_code: int | None = None
    cache_hit: bool | None = None
    handler_started_at: float | None = None

    def record(self, name: str, duration_ms: float, *, stage: str | None = None) -> None:
        duration_ms = max(duration_ms, 0.0)
        self.durations_ms[name] = self.durations_ms.get(name, 0.0) + duration_ms
        if name in _DB_PHASES:
            db_total = f"{stage or current_request_stage()}_db"
            self.durations_ms[db_total] = (
                self.durations_ms.get(db_total, 0.0) + duration_ms
            )
        if name == "db_sql":
            self.query_count += 1
            if (stage or current_request_stage()) == "handler":
                self.handler_query_count += 1


_request_timing: ContextVar[RequestTiming | None] = ContextVar(
    "oddish_request_timing", default=None
)
_request_stage: ContextVar[str] = ContextVar("oddish_request_stage", default="handler")


def begin_request_timing() -> tuple[RequestTiming, Token, Token]:
    timing = RequestTiming()
    return timing, _request_timing.set(timing), _request_stage.set("handler")


def reset_request_timing(tokens: tuple[Token, Token]) -> None:
    timing_token, stage_token = tokens
    _request_stage.reset(stage_token)
    _request_timing.reset(timing_token)


def current_request_timing() -> RequestTiming | None:
    return _request_timing.get()


def current_request_stage() -> str:
    return _request_stage.get()


def begin_auth_timing() -> tuple[float, Token]:
    return now(), _request_stage.set("auth")


def finish_auth_timing(auth_timing: tuple[float, Token]) -> None:
    started_at, token = auth_timing
    timing = current_request_timing()
    if timing is not None:
        timing.record("auth_total", elapsed_ms(started_at), stage="auth")
    _request_stage.reset(token)
    if timing is not None:
        timing.handler_started_at = now()


def record_cache_result(*, hit: bool) -> None:
    timing = current_request_timing()
    if timing is not None:
        timing.cache_hit = hit


@contextmanager
def timed_phase(name: str, **attributes: Any):
    from oddish.observability import span

    stage = current_request_stage()
    started_at = now()
    try:
        with span(f"backend.{name.replace('_', '.')}", **attributes):
            yield
    finally:
        timing = current_request_timing()
        if timing is not None:
            timing.record(name, elapsed_ms(started_at), stage=stage)


def finish_handler_timing(timing: RequestTiming) -> None:
    if "handler_total" in timing.durations_ms:
        return
    started_at = timing.handler_started_at or timing.started_at
    timing.record("handler_total", elapsed_ms(started_at), stage="handler")


def request_phase_metrics(timing: RequestTiming) -> list[TimingMetric]:
    return [
        (name, timing.durations_ms.get(name, 0.0), description)
        for name, description in _PHASE_DESCRIPTIONS.items()
    ]


def now() -> float:
    return perf_counter()


def elapsed_ms(started_at: float) -> float:
    return max((perf_counter() - started_at) * 1000.0, 0.0)


def _sanitize_metric_name(name: str) -> str:
    sanitized = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in name)
    return sanitized or "metric"


def _quote_description(description: str) -> str:
    return description.replace("\\", "\\\\").replace('"', '\\"')


def format_server_timing(metrics: list[TimingMetric]) -> str:
    parts: list[str] = []
    for name, duration_ms, description in metrics:
        metric = f"{_sanitize_metric_name(name)};dur={max(duration_ms, 0.0):.1f}"
        if description:
            metric += f';desc="{_quote_description(description)}"'
        parts.append(metric)
    return ", ".join(parts)


def join_server_timing_headers(*headers: str | None) -> str | None:
    joined = ", ".join(
        header.strip() for header in headers if header and header.strip()
    )
    return joined or None


def add_server_timing_metric(
    request: Any,
    name: str,
    duration_ms: float,
    description: str | None = None,
) -> None:
    state = getattr(request, "state", None)
    if state is None:
        return

    metrics = getattr(state, "server_timing_metrics", None)
    if metrics is None:
        metrics = []
        setattr(state, "server_timing_metrics", metrics)

    metrics.append((name, duration_ms, description))
