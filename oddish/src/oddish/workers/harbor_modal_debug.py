from __future__ import annotations

import contextlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterator, TextIO

from harbor.models.environment_type import EnvironmentType

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class _TeeTextIO:
    """Mirror terminal output to a debug log file."""

    def __init__(self, primary: TextIO, secondary: TextIO) -> None:
        self._primary = primary
        self._secondary = secondary

    def write(self, data: str) -> int:
        self._primary.write(data)
        cleaned = (
            _ANSI_ESCAPE_RE.sub("", data).replace("\r\n", "\n").replace("\r", "\n")
        )
        if cleaned:
            self._secondary.write(cleaned)
        return len(data)

    def flush(self) -> None:
        self._primary.flush()
        self._secondary.flush()

    def isatty(self) -> bool:
        isatty = getattr(self._primary, "isatty", None)
        return bool(isatty and isatty())

    @property
    def encoding(self) -> str | None:
        return getattr(self._primary, "encoding", None)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._primary, name)


@contextlib.contextmanager
def _capture_modal_output(
    job_dir: Path, environment: EnvironmentType
) -> Iterator[Path | None]:
    """Capture Modal SDK output into a trial-local log file."""
    if environment != EnvironmentType.MODAL:
        yield None
        return

    log_path = job_dir / "modal-output.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with contextlib.ExitStack() as stack:
        log_file = stack.enter_context(log_path.open("a", encoding="utf-8"))
        log_file.write(
            "[oddish] Capturing Modal SDK output for this trial. "
            "Image build failures will usually appear here.\n"
        )
        log_file.flush()

        stack.enter_context(
            contextlib.redirect_stdout(_TeeTextIO(sys.stdout, log_file))  # type: ignore[type-var]
        )
        stack.enter_context(
            contextlib.redirect_stderr(_TeeTextIO(sys.stderr, log_file))  # type: ignore[type-var]
        )

        try:
            import modal
        except Exception as exc:
            log_file.write(
                f"[oddish] Failed to enable modal output capture: {type(exc).__name__}: {exc}\n"
            )
            log_file.flush()
            yield log_path
            return

        output_manager = stack.enter_context(modal.enable_output())
        if hasattr(output_manager, "enable_image_logs"):
            output_manager.enable_image_logs()
        if hasattr(output_manager, "set_timestamps"):
            output_manager.set_timestamps(True)

        yield log_path


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
