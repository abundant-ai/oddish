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
