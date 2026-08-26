"""Structured failure metadata at the Harbor-to-Oddish boundary.

Harbor records a Python exception type and message.  Oddish also owns phase
timing and the downloaded agent logs, so this is the first layer with enough
information to distinguish environment, installer, provider, verifier, and
runtime failures without asking API or UI clients to parse prose.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from harbor.agents.installed.base import (
    AgentAuthenticationError,
    ApiInternalServerError,
    ApiOverloadedError,
    ApiRateLimitError,
    ModelNotFoundError,
    NonZeroAgentExitCodeError,
    UnknownApiError,
)
from harbor.models.job.config import RetryConfig

_API_ERROR_MESSAGE_RE = re.compile(r"\bAPI error\b", re.IGNORECASE)
_API_STATUS_MESSAGE_RE = re.compile(
    r"\b(?:API error:\s*)?([45]\d\d)\b", re.IGNORECASE
)
_RETRY_EXCLUDED_EXCEPTION_TYPES = frozenset(
    RetryConfig.model_fields["exclude_exceptions"].default_factory() or set()
)
_UNSTATUS_RETRY_REASON_BY_EXCEPTION = {
    "ApiRateLimitError": "rate_limit",
    "ApiOverloadedError": "provider_overload",
    "ApiInternalServerError": "provider_server_error",
    "ApiConnectionClosedError": "provider_server_error",
    "ApiResponseStalledError": "provider_server_error",
}


@dataclass(frozen=True)
class ClaudeProviderFailure:
    """Provider failure reported by Claude Code's final stream-json event."""

    terminal_reason: str
    http_status: int | None = None
    request_id: str | None = None
    session_id: str | None = None
    retry_after_seconds: float | None = None
    message: str | None = None

    @property
    def retryable(self) -> bool:
        """Whether a second provider request can change the outcome."""
        if self.http_status is None:
            return True
        return self.http_status == 429 or self.http_status >= 500

    @property
    def retry_reason(self) -> str:
        if self.http_status == 429:
            return "rate_limit"
        if self.http_status == 529:
            return "provider_overload"
        if self.http_status is not None and 500 <= self.http_status < 600:
            return "provider_server_error"
        return "provider_api"

    @property
    def exception_class(self) -> type[NonZeroAgentExitCodeError]:
        if self.http_status in {401, 403}:
            return AgentAuthenticationError
        if self.http_status == 404:
            return ModelNotFoundError
        if self.http_status == 429:
            return ApiRateLimitError
        if self.http_status == 529:
            return ApiOverloadedError
        if self.http_status is not None and 500 <= self.http_status < 600:
            return ApiInternalServerError
        return UnknownApiError

    def summary(self) -> str:
        fields = [f"terminal_reason={self.terminal_reason}"]
        if self.http_status is not None:
            fields.append(f"http_status={self.http_status}")
        if self.request_id:
            fields.append(f"request_id={self.request_id}")
        if self.session_id:
            fields.append(f"session_id={self.session_id}")
        return f"Claude provider API failure ({', '.join(fields)})"

    def as_failure_info(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "category": "provider_api",
            "phase": "agent_execution",
            "code": (
                f"http_{self.http_status}"
                if self.http_status is not None
                else self.terminal_reason
            ),
            "retryable": self.retryable,
            "retry_reason": self.retry_reason,
            "terminal_reason": self.terminal_reason,
        }
        for key, value in asdict(self).items():
            if key != "terminal_reason" and value is not None:
                result[key] = value
        return result


class HarborOutcomeLike(Protocol):
    error: str | None
    exception_type: str | None
    phase_timing: dict[str, Any] | None
    job_dir: Path | None


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _coerce_positive_seconds(event: dict[str, Any]) -> float | None:
    milliseconds = event.get("retry_after_ms", event.get("retryAfterMs"))
    try:
        if milliseconds is not None:
            return max(0.0, float(milliseconds) / 1000.0)
        seconds = event.get("retry_after", event.get("retryAfter"))
        return max(0.0, float(seconds)) if seconds is not None else None
    except (TypeError, ValueError):
        return None


def _nested_string(value: Any, *keys: str) -> str | None:
    """Return the first named string from a small structured event tree."""
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        for child in value.values():
            candidate = _nested_string(child, *keys)
            if candidate:
                return candidate
    elif isinstance(value, list):
        for child in value:
            candidate = _nested_string(child, *keys)
            if candidate:
                return candidate
    return None


def _event_failure(event: Any) -> ClaudeProviderFailure | None:
    if not isinstance(event, dict) or event.get("type") != "result":
        return None

    is_error = event.get("is_error", event.get("isError"))
    terminal_reason = event.get("terminal_reason", event.get("terminalReason"))
    status = _coerce_int(
        event.get(
            "api_error_status",
            event.get("apiErrorStatus", event.get("http_status")),
        )
    )
    message_value = event.get("result")
    message = message_value if isinstance(message_value, str) else None
    if message is None:
        message = _nested_string(event.get("error"), "message")

    # Claude uses subtype="success" for a clean protocol shutdown even when
    # the final result is an API failure. ``is_error`` is the success/failure
    # discriminator; terminal_reason/status retain compatibility with result
    # frames from CLI versions that did not emit it.
    if is_error is False:
        return None
    has_api_error_signal = (
        terminal_reason == "api_error"
        or status is not None
        or is_error is True
        and message is not None
        and _API_ERROR_MESSAGE_RE.search(message) is not None
    )
    if not has_api_error_signal:
        return None

    if status is None and message:
        status_match = _API_STATUS_MESSAGE_RE.search(message)
        if status_match:
            status = int(status_match.group(1))
    return ClaudeProviderFailure(
        terminal_reason=str(terminal_reason or "api_error"),
        http_status=status,
        request_id=_nested_string(event, "request_id", "requestId"),
        session_id=_nested_string(event, "session_id", "sessionId"),
        retry_after_seconds=_coerce_positive_seconds(event),
        message=message,
    )


def parse_claude_provider_failure(*streams: str | None) -> ClaudeProviderFailure | None:
    """Parse the last Claude result event and return it only when it failed."""
    last_result: dict[str, Any] | None = None
    for stream in streams:
        for raw_line in (stream or "").splitlines():
            line = raw_line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                last_result = event
    return _event_failure(last_result)


def read_claude_provider_failure(job_dir: Path | None) -> ClaudeProviderFailure | None:
    """Read the final Claude result event from a downloaded Harbor job tree."""
    if job_dir is None or not job_dir.exists():
        return None
    candidates = sorted(job_dir.rglob("agent/claude-code.txt"))
    if not candidates:
        candidates = sorted(job_dir.rglob("claude-code.txt"))
    for path in reversed(candidates):
        try:
            failure = parse_claude_provider_failure(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if failure is not None:
            return failure
    return None


def classify_harbor_failure(outcome: HarborOutcomeLike) -> dict[str, Any] | None:
    """Normalize one failed Harbor outcome into a stable application contract."""
    if not outcome.error and not outcome.exception_type:
        return None

    exception_type = outcome.exception_type or "UnknownError"
    provider_failure = read_claude_provider_failure(outcome.job_dir)
    if provider_failure is not None:
        failure_info = provider_failure.as_failure_info()
        if provider_failure.http_status is None:
            failure_info["retryable"] = (
                exception_type not in _RETRY_EXCLUDED_EXCEPTION_TYPES
            )
            failure_info["retry_reason"] = (
                _UNSTATUS_RETRY_REASON_BY_EXCEPTION.get(exception_type)
                or failure_info["retry_reason"]
            )
        return failure_info

    timing = outcome.phase_timing or {}
    error = outcome.error or ""
    is_permanent_image_build = "Image build for im-" in error
    if is_permanent_image_build:
        category, phase = "environment_build", "environment_setup"
    elif exception_type.startswith("Verifier"):
        category, phase = "verifier", "verifier"
    elif (
        exception_type.startswith("Api")
        or exception_type == "UnknownApiError"
        or exception_type == "NetworkConnectionError"
        and "agent_execution" in timing
    ):
        category, phase = "provider_api", "agent_execution"
    elif "agent_execution" in timing:
        category, phase = "agent_execution", "agent_execution"
    elif "agent_setup" in timing:
        category, phase = "agent_install", "agent_setup"
    elif "environment_setup" in timing:
        category, phase = "environment_build", "environment_setup"
    elif "verifier" in timing:
        category, phase = "verifier", "verifier"
    else:
        category, phase = "runtime_lifecycle", "runtime"

    return {
        "category": category,
        "phase": phase,
        "code": exception_type,
        "retryable": exception_type not in _RETRY_EXCLUDED_EXCEPTION_TYPES
        and not is_permanent_image_build,
        "retry_reason": category,
    }
