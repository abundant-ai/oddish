from __future__ import annotations

from time import perf_counter
from typing import Any, Callable

TimingMetric = tuple[str, float, str | None]
TimingRecorder = Callable[[str, float, str | None], None]


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

    # TEMP profiling (remove post-testing): mirror each stage timing into
    # Logfire so the dashboard request trace shows per-stage durations
    # (auth_jwt / auth_db / db_connect / dashboard_queue_pipeline /
    # dashboard_usage / dashboard_experiments_total / dashboard_total, ...).
    # Guarded + best-effort: a no-op when Logfire isn't installed/configured.
    try:
        import logfire

        logfire.info(
            "server_timing {metric}",
            metric=name,
            duration_ms=round(duration_ms, 1),
            description=description,
        )
    except Exception:
        pass
