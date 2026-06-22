from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

from harbor.models.environment_type import EnvironmentType


def _capture_modal_output(
    job_dir: Path, environment: EnvironmentType
) -> contextlib.AbstractContextManager:
    """Capture Modal SDK output into a trial-local log file.

    Modal-specific capture now lives in ``ModalBackend.capture_diagnostics``;
    this remains the call site used by ``harbor_runner`` and dispatches to it
    only for the Modal environment (no-op otherwise)."""
    if environment != EnvironmentType.MODAL:
        return contextlib.nullcontext(None)
    from oddish.runtime.backends.modal import ModalBackend

    return ModalBackend().capture_diagnostics(job_dir)


def _write_debug_result_json(
    *,
    job_dir: Path,
    duration_sec: float,
    exception_type: str,
    exception_message: str,
    debug_log_path: Path | None = None,
) -> Path:
    """Persist a minimal result.json when Harbor fails before writing one."""
    result_path = job_dir / "result.json"
    payload: dict[str, Any] = {
        "trial_results": [],
        "duration_sec": round(duration_sec, 2),
        "exception_info": {
            "exception_type": exception_type,
            "exception_message": exception_message,
        },
        "debug_artifacts": {},
    }
    if debug_log_path is not None:
        payload["debug_artifacts"]["modal_output_log"] = debug_log_path.name
    result_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    return result_path


def _maybe_add_modal_debug_hint(error_message: str, debug_log_path: Path | None) -> str:
    """Append a short pointer to the captured Modal debug log."""
    if debug_log_path is None:
        return error_message
    return (
        f"{error_message} Captured Modal SDK output in {debug_log_path.name}; "
        "open the trial logs to inspect the image build failure."
    )


def _format_exception_message(exc: BaseException) -> str:
    """Return a concise exception summary, including ExceptionGroup children."""
    base = f"{type(exc).__name__}: {exc}"
    if not isinstance(exc, BaseExceptionGroup) or not exc.exceptions:
        return base

    child_summaries = [
        f"{type(child).__name__}: {child}" for child in exc.exceptions[:3]
    ]
    if len(exc.exceptions) > 3:
        child_summaries.append(f"+{len(exc.exceptions) - 3} more")
    return f"{base} ({'; '.join(child_summaries)})"
