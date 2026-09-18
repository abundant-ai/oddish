"""Forward a trial's model spend to the execution backend that can act on it.

oddish already knows two things the sandbox provider does not: how much the
model has cost a trial so far (the live tailer prices every usage event), and
when an org's quota says the trial must stop spending (quota control). A
provider that can pause a sandbox with its memory intact turns those two
facts into "the trial waits" instead of "the trial dies". Numinous does; the
signal is ``report_spend`` on its backend. Any backend without the method is
silently skipped, so this costs nothing where it cannot help.

Never raises. A failed signal is logged and the trial proceeds exactly as it
would have without it.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _provider_name(environment: Any) -> str | None:
    """A provider name from whatever the caller has: an EnvironmentType, its
    string value, or a live harbor environment (which knows its own type)."""
    if environment is None:
        return None
    if isinstance(environment, str):
        return environment
    value = getattr(environment, "value", None)
    if isinstance(value, str):
        return value
    type_fn = getattr(environment, "type", None)
    if callable(type_fn):
        try:
            t = type_fn()
        except Exception:  # noqa: BLE001 - metadata only
            return None
        return getattr(t, "value", None) or (t if isinstance(t, str) else None)
    return None


async def signal_spend(
    environment: Any,
    trial_id: str,
    *,
    kind: str = "usage",
    cumulative_usd: float | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    note: dict[str, Any] | None = None,
) -> bool:
    """Send one spend signal for a trial. ``kind`` is ``usage``,
    ``credit_exhausted`` or ``credit_restored``. Returns whether a backend
    accepted it; False also when no backend for this provider can."""
    if not trial_id:
        return False
    provider = _provider_name(environment)
    if not provider:
        return False
    try:
        from oddish.runtime.registry import get_backend

        backend = get_backend(provider)
    except Exception:  # noqa: BLE001
        return False
    report = getattr(backend, "report_spend", None)
    if report is None:
        return False
    try:
        return bool(
            await report(
                trial_id,
                kind=kind,
                cumulative_usd=cumulative_usd,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                note=note,
            )
        )
    except Exception:  # noqa: BLE001 - must never fail a trial
        logger.info(
            "metric=spend_signal.raised trial_id=%s provider=%s kind=%s",
            trial_id,
            provider,
            kind,
        )
        return False
