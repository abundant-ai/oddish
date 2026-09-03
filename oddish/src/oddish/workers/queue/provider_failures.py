"""Which provider-API errors are worth retrying.

Sibling of ``trial_failures``: a narrow predicate the job handlers use to pick
``retryable`` on a ``JobOutcome``, and settlement uses to refuse SUCCESS for
model-setup failures that still produced a verifier reward of 0.
"""

from __future__ import annotations

import re

# Harbor class names plus LiteLLM/OpenAI ``NotFoundError`` (not in Harbor's
# default exclude list, so Oddish must treat it as permanent itself).
PERMANENT_MODEL_SETUP_EXCEPTION_TYPES: frozenset[str] = frozenset(
    {
        "ModelNotFoundError",
        "AgentAuthenticationError",
        "NotFoundError",
    }
)

_PERMANENT_PROVIDER_RE = re.compile(
    r"PermissionDeniedError|Error code:\s*403\b|"
    r"Model not found|NotFoundError|model_not_found|"
    r"does not exist or your team does not have access|"
    r"AgentAuthenticationError|Not logged in",
    re.IGNORECASE,
)


def is_permanent_provider_failure(error: str | None) -> bool:
    """True when retrying ``error`` cannot plausibly succeed."""
    if not error:
        return False
    return bool(_PERMANENT_PROVIDER_RE.search(error))


def is_permanent_model_setup_exception(exception_type: str | None) -> bool:
    """True for provider NotFound / auth exception class names."""
    if not exception_type:
        return False
    return exception_type in PERMANENT_MODEL_SETUP_EXCEPTION_TYPES


def trial_did_real_agent_work(
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    has_trajectory: bool | None,
    total_steps: int | None,
) -> bool:
    """True when settlement should treat the run as a real eval, not setup miss.

    Shared by Harbor settlement and QA eligibility so a NotFound/auth marker
    after real agent work stays QA-visible.
    """
    tokens = (input_tokens or 0) + (output_tokens or 0)
    if tokens > 0:
        return True
    if has_trajectory:
        return True
    if total_steps:
        return True
    return False
