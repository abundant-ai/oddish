"""Typed failure evidence and policy at the Harbor-to-Oddish boundary.

Agent adapters may persist ``provider-failure.json`` when their final process
reports structured provider evidence. This module combines that evidence with
Harbor's exception type and phase timing exactly once. The resulting
``FailureInfo`` drives both trial retry state and the diagnostic payload stored
in ``trials.result``.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from harbor.models.job.config import RetryConfig

PROVIDER_FAILURE_FILENAME = "provider-failure.json"
FAILURE_INFO_SCHEMA_VERSION = 1

_MAX_CODE_LENGTH = 120
_MAX_IDENTIFIER_LENGTH = 256
_MAX_SUMMARY_LENGTH = 500
_MAX_RETRY_AFTER_SECONDS = 960.0
_MODAL_IMAGE_BUILD_FAILED_RE = re.compile(
    r"\bImage build for im-[^\s]+ failed\b",
    re.IGNORECASE,
)
_NON_RETRYABLE_EXCEPTION_TYPES = frozenset(
    RetryConfig.model_fields["exclude_exceptions"].default_factory() or set()
) | {
    "HarborOverrideImportError",
    "QuotaPauseControlError",
}
_TRANSIENT_PROVIDER_REASON_BY_EXCEPTION = {
    "ApiRateLimitError": "rate_limit",
    "ApiOverloadedError": "provider_overload",
    "ApiInternalServerError": "provider_server_error",
    "ApiConnectionClosedError": "provider_server_error",
    "ApiResponseStalledError": "provider_server_error",
}


def _bounded_string(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] if value else None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return min(max(0.0, number), _MAX_RETRY_AFTER_SECONDS)


def _optional_http_status(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


@dataclass(frozen=True)
class ProviderFailureEvidence:
    """Provider facts emitted by one harness's final failed invocation."""

    provider: str
    terminal_reason: str
    http_status: int | None = None
    request_id: str | None = None
    resume_token: str | None = None
    retry_after_seconds: float | None = None
    summary: str | None = None

    def __post_init__(self) -> None:
        provider = _bounded_string(self.provider, _MAX_CODE_LENGTH)
        terminal_reason = _bounded_string(self.terminal_reason, _MAX_CODE_LENGTH)
        if provider is None or terminal_reason is None:
            raise ValueError("Provider failure requires provider and terminal_reason")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "terminal_reason", terminal_reason)
        object.__setattr__(self, "http_status", _optional_http_status(self.http_status))
        object.__setattr__(
            self,
            "request_id",
            _bounded_string(self.request_id, _MAX_IDENTIFIER_LENGTH),
        )
        object.__setattr__(
            self,
            "resume_token",
            _bounded_string(self.resume_token, _MAX_IDENTIFIER_LENGTH),
        )
        object.__setattr__(
            self,
            "retry_after_seconds",
            _optional_float(self.retry_after_seconds),
        )
        object.__setattr__(
            self,
            "summary",
            _bounded_string(self.summary, _MAX_SUMMARY_LENGTH),
        )

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": FAILURE_INFO_SCHEMA_VERSION,
            "provider": self.provider,
            "terminal_reason": self.terminal_reason,
        }
        optional = {
            "http_status": self.http_status,
            "request_id": self.request_id,
            "resume_token": self.resume_token,
            "retry_after_seconds": self.retry_after_seconds,
            "summary": self.summary,
        }
        result.update(
            {key: value for key, value in optional.items() if value is not None}
        )
        return result

    @classmethod
    def from_dict(cls, value: Any) -> ProviderFailureEvidence | None:
        if not isinstance(value, dict):
            return None
        if value.get("schema_version") != FAILURE_INFO_SCHEMA_VERSION:
            return None
        provider = _bounded_string(value.get("provider"), _MAX_CODE_LENGTH)
        terminal_reason = _bounded_string(
            value.get("terminal_reason"), _MAX_CODE_LENGTH
        )
        if provider is None or terminal_reason is None:
            return None
        return cls(
            provider=provider,
            terminal_reason=terminal_reason,
            http_status=_optional_http_status(value.get("http_status")),
            request_id=_bounded_string(value.get("request_id"), _MAX_IDENTIFIER_LENGTH),
            resume_token=_bounded_string(
                value.get("resume_token"), _MAX_IDENTIFIER_LENGTH
            ),
            retry_after_seconds=_optional_float(value.get("retry_after_seconds")),
            summary=_bounded_string(value.get("summary"), _MAX_SUMMARY_LENGTH),
        )


@dataclass(frozen=True)
class FailureInfo:
    """One authoritative Oddish decision for a failed Harbor outcome."""

    category: str
    phase: str
    code: str
    retryable: bool
    retry_reason: str
    provider: str | None = None
    terminal_reason: str | None = None
    http_status: int | None = None
    request_id: str | None = None
    session_id: str | None = None
    retry_after_seconds: float | None = None
    summary: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": FAILURE_INFO_SCHEMA_VERSION,
            "category": self.category,
            "phase": self.phase,
            "code": self.code,
            "retryable": self.retryable,
            "retry_reason": self.retry_reason,
        }
        optional = {
            "provider": _bounded_string(self.provider, _MAX_CODE_LENGTH),
            "terminal_reason": _bounded_string(self.terminal_reason, _MAX_CODE_LENGTH),
            "http_status": _optional_http_status(self.http_status),
            "request_id": _bounded_string(self.request_id, _MAX_IDENTIFIER_LENGTH),
            "session_id": _bounded_string(self.session_id, _MAX_IDENTIFIER_LENGTH),
            "retry_after_seconds": _optional_float(self.retry_after_seconds),
            "summary": _bounded_string(self.summary, _MAX_SUMMARY_LENGTH),
        }
        result.update(
            {key: value for key, value in optional.items() if value is not None}
        )
        return result


class HarborOutcomeLike(Protocol):
    @property
    def error(self) -> str | None: ...

    @property
    def exception_type(self) -> str | None: ...

    @property
    def phase_timing(self) -> dict[str, Any] | None: ...

    @property
    def job_dir(self) -> Path | None: ...


def is_modal_image_build_failure(error: str | None) -> bool:
    """Whether Modal reported its exact permanent image-build failure."""
    return bool(error and _MODAL_IMAGE_BUILD_FAILED_RE.search(error))


def _read_provider_failure(job_dir: Path | None) -> ProviderFailureEvidence | None:
    if job_dir is None or not job_dir.exists():
        return None
    candidates = sorted(job_dir.rglob(f"agent/{PROVIDER_FAILURE_FILENAME}"))
    if not candidates:
        candidates = sorted(job_dir.rglob(PROVIDER_FAILURE_FILENAME))
    for path in reversed(candidates):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        evidence = ProviderFailureEvidence.from_dict(payload)
        if evidence is not None:
            return evidence
    return None


def classify_provider_failure(
    evidence: ProviderFailureEvidence,
    *,
    exception_type: str,
) -> FailureInfo:
    """Apply shared retry policy to structured provider evidence."""
    status = _optional_http_status(evidence.http_status)
    if status == 429:
        retryable, retry_reason = True, "rate_limit"
    elif status == 529:
        retryable, retry_reason = True, "provider_overload"
    elif status is not None and 500 <= status < 600:
        retryable, retry_reason = True, "provider_server_error"
    elif status is not None:
        retryable, retry_reason = False, "provider_api"
    else:
        retry_reason = _TRANSIENT_PROVIDER_REASON_BY_EXCEPTION.get(
            exception_type, "provider_api"
        )
        retryable = exception_type in _TRANSIENT_PROVIDER_REASON_BY_EXCEPTION

    return FailureInfo(
        category="provider_api",
        phase="agent_execution",
        code=f"http_{status}" if status is not None else evidence.terminal_reason,
        retryable=retryable,
        retry_reason=retry_reason,
        provider=evidence.provider,
        terminal_reason=evidence.terminal_reason,
        http_status=status,
        request_id=evidence.request_id,
        session_id=evidence.resume_token,
        retry_after_seconds=_optional_float(evidence.retry_after_seconds),
        summary=evidence.summary,
    )


def classify_harbor_failure(outcome: HarborOutcomeLike) -> FailureInfo | None:
    """Normalize one failed Harbor outcome into a stable application contract."""
    if not outcome.error and not outcome.exception_type:
        return None

    exception_type = outcome.exception_type or "UnknownError"
    provider_failure = _read_provider_failure(outcome.job_dir)
    if provider_failure is not None:
        return classify_provider_failure(
            provider_failure,
            exception_type=exception_type,
        )

    timing = outcome.phase_timing or {}
    error = outcome.error or ""
    image_build_failure = is_modal_image_build_failure(error)
    if image_build_failure:
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

    retryable = (
        exception_type not in _NON_RETRYABLE_EXCEPTION_TYPES and not image_build_failure
    )
    return FailureInfo(
        category=category,
        phase=phase,
        code=exception_type,
        retryable=retryable,
        retry_reason=category,
    )
