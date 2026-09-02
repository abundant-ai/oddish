"""Normalize provider error strings at the external process boundary."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

MAX_PROVIDER_ERROR_LENGTH = 500
_HTTP_STATUS_RE = re.compile(
    r"(?:API Error|Error code|HTTP(?: status)?(?: code)?)\s*:?\s*([1-5]\d{2})\b",
    re.IGNORECASE,
)
_BARE_RATE_LIMIT_STATUS_RE = re.compile(r"\b429\b")
_RECOVERY_AT_RE = re.compile(
    r"regain access on\s+(\d{4}-\d{2}-\d{2})\s+at\s+(\d{2}:\d{2})(?::\d{2})?\s+UTC\b",
    re.IGNORECASE,
)
_RATE_LIMIT_RE = re.compile(
    r"\b("
    r"too many requests|"
    r"rate[\s_-]*limit(?:ed|s|ing)?|"
    r"ratelimit(?:ed|s|ing)?|"
    r"quota(?: exceeded)?|"
    r"resource[_\s-]*exhausted|"
    r"requests per minute|"
    r"tokens per minute|"
    r"throttl(?:ed|ing)?"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ProviderFailureClassification:
    """Bounded provider facts safe to attach to logs and retry decisions."""

    failure_class: str
    provider_status_code: int | None
    recovery_at: datetime | None
    error_summary: str | None


def classify_provider_failure(
    error: str | None,
    *,
    http_status: int | None = None,
    exception_type: str | None = None,
) -> ProviderFailureClassification:
    """Classify one untrusted provider/CLI error without raising."""
    normalized = " ".join((error or "").split())
    error_summary: str | None
    if len(normalized) > MAX_PROVIDER_ERROR_LENGTH:
        error_summary = normalized[: MAX_PROVIDER_ERROR_LENGTH - 3] + "..."
    else:
        error_summary = normalized or None

    status_match = _HTTP_STATUS_RE.search(normalized)
    status_code = http_status
    if status_code is None and status_match:
        status_code = int(status_match.group(1))
    if status_code is None and _BARE_RATE_LIMIT_STATUS_RE.search(normalized):
        # Preserve staging's standalone-429 behavior for CLI errors that do
        # not expose a structured HTTP status.
        status_code = 429

    recovery_at = None
    recovery_match = _RECOVERY_AT_RE.search(normalized)
    if recovery_match:
        try:
            recovery_at = datetime.strptime(
                " ".join(recovery_match.groups()), "%Y-%m-%d %H:%M"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            recovery_at = None

    lowered = normalized.lower()
    lowered_exception_type = (exception_type or "").lower()
    if "api usage limit" in lowered and "reached" in lowered:
        failure_class = "provider_usage_limit"
    elif (
        status_code == 403
        or "permissiondeniederror" in lowered
        or "permissiondeniederror" in lowered_exception_type
    ):
        failure_class = "permission_denied"
    elif "credit balance is too low" in lowered or "insufficient credit" in lowered:
        failure_class = "low_credit"
    elif (
        "input tokens exceed" in lowered
        or "token limit" in lowered
        or "context length" in lowered
    ):
        failure_class = "token_limit"
    elif status_code == 429 or _RATE_LIMIT_RE.search(normalized):
        failure_class = "rate_limit"
    elif (
        "timeouterror" in lowered
        or "timeouterror" in lowered_exception_type
        or "timed out" in lowered
        or "timeout" in lowered
    ):
        failure_class = "timeout"
    elif status_code is not None and 500 <= status_code < 600:
        failure_class = "provider_unavailable"
    elif status_code is not None:
        failure_class = "provider_error"
    else:
        failure_class = "unknown"

    return ProviderFailureClassification(
        failure_class=failure_class,
        provider_status_code=status_code,
        recovery_at=recovery_at,
        error_summary=error_summary,
    )
