from __future__ import annotations

from oddish.workers.harbor.failure_info import is_modal_image_build_failure

MODAL_IMAGE_BUILD_FAILED_STAGE = "image_build_failed"

__all__ = ["MODAL_IMAGE_BUILD_FAILED_STAGE", "is_modal_image_build_failure"]
