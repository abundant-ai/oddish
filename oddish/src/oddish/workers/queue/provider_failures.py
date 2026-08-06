"""Which provider-API errors are worth retrying.

Sibling of ``trial_failures``: a narrow, string-matched predicate the job
handlers use to pick ``retryable`` on a ``JobOutcome``.

Only authorization-class failures (HTTP 403 / ``PermissionDeniedError``) count
as permanent. A permission block is a property of the credential or the
resource, not of the request, so re-sending the same call cannot clear it --
whereas 429s, 400s (token-limit, low credit balance) and timeouts all recover
on their own and must keep their retries. Azure calls its content-policy
resource block "temporary", but it outlives a job's whole retry budget by
days, so it belongs on the permanent side.
"""

from __future__ import annotations

import re

_PERMANENT_PROVIDER_RE = re.compile(
    r"PermissionDeniedError|Error code:\s*403\b",
    re.IGNORECASE,
)


def is_permanent_provider_failure(error: str | None) -> bool:
    """True when retrying ``error`` cannot plausibly succeed."""
    if not error:
        return False
    return bool(_PERMANENT_PROVIDER_RE.search(error))
