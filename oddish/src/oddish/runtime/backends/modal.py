"""Modal execution backend. ``import modal`` is lazy (confined to the methods
that need it) so importing this module never pulls the SDK into a process
that lacks it."""

from __future__ import annotations

import contextlib
import logging
import re
import sys
from pathlib import Path
from typing import Any, Iterator

from oddish.runtime.ports import Capabilities, ExecutionBackend, GpuSupport

logger = logging.getLogger(__name__)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class _TeeTextIO:
    """Mirror terminal output to a debug log file."""

    def __init__(self, primary, secondary) -> None:
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
    def encoding(self):
        return getattr(self._primary, "encoding", None)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._primary, name)


class ModalBackend:
    name = "modal"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            gpu=GpuSupport(
                accelerators=("H100", "H200", "A100", "L40S", "A10G", "T4"),
                max_count=8,
            ),
            private_registry_pull=True,
            network_egress="configurable",
            persistent_volumes=False,
            streaming_logs=True,
            memory_snapshot_fork=True,
            cold_start="minutes",
        )

    def harbor_env_kwargs(self, base_kwargs: dict[str, Any]) -> dict[str, Any]:
        # Modal needs no extra env kwargs today; pass the caller's through.
        return dict(base_kwargs)

    async def teardown(self, external_id: str) -> bool:
        if not external_id:
            return False
        try:
            import modal

            sandbox = await modal.Sandbox.from_id.aio(external_id)
            await sandbox.terminate.aio()
        except Exception:
            logger.exception(
                "ModalBackend.teardown: failed to terminate %s", external_id
            )
            return False
        logger.info("ModalBackend.teardown: terminated %s", external_id)
        return True

    @contextlib.contextmanager
    def capture_diagnostics(self, job_dir: Path) -> Iterator[Path | None]:
        """Capture Modal SDK output into a trial-local log file."""
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
                contextlib.redirect_stdout(_TeeTextIO(sys.stdout, log_file))
            )
            stack.enter_context(
                contextlib.redirect_stderr(_TeeTextIO(sys.stderr, log_file))
            )

            try:
                import modal
            except Exception as exc:
                log_file.write(
                    f"[oddish] Failed to enable modal output capture: "
                    f"{type(exc).__name__}: {exc}\n"
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


_: ExecutionBackend = ModalBackend()  # structural conformance check at import
