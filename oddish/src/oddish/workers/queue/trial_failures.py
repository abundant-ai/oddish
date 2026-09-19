from __future__ import annotations

import re

MODAL_IMAGE_BUILD_FAILED_STAGE = "image_build_failed"

_MODAL_IMAGE_BUILD_FAILED_RE = re.compile(
    r"\bImage build for im-[^\s]+ failed\b",
    re.IGNORECASE,
)


def is_modal_image_build_failure(error: str | None) -> bool:
    if not error:
        return False
    return bool(_MODAL_IMAGE_BUILD_FAILED_RE.search(error))


# A trial waiting for provider capacity is queued, not stuck. Without a stage
# of its own the wait renders as "starting", which is indistinguishable from
# a hung trial.
WAITING_FOR_CAPACITY_STAGE = "waiting_for_capacity"

_WAITING_FOR_CAPACITY_RE = re.compile(
    r"waiting for capacity, retrying in (?P<eta>\d+(?:\.\d+)?)s",
    re.IGNORECASE,
)


def waiting_for_capacity_eta(text: str | None) -> float | None:
    """Seconds the provider says it needs, or None if not waiting."""
    if not text:
        return None
    match = _WAITING_FOR_CAPACITY_RE.search(text)
    return float(match.group("eta")) if match else None


def is_waiting_for_capacity(text: str | None) -> bool:
    return waiting_for_capacity_eta(text) is not None
