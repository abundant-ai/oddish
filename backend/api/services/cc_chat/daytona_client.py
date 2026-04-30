from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Protocol


logger = logging.getLogger("cc_chat.daytona")

from daytona import (
    AsyncDaytona,
    CreateSandboxFromSnapshotParams,
    DaytonaConfig,
    SessionExecuteRequest,
)


@dataclass
class CreatedSandbox:
    """Just the bits the orchestrator needs to keep around."""

    id: str
    _sdk_handle: object  # opaque; used internally


class DaytonaClient(Protocol):
    async def create_sandbox(
        self, *, env_vars: dict[str, str], auto_stop_minutes: int
    ) -> CreatedSandbox: ...

    async def upload_file(
        self, sandbox: CreatedSandbox, *, dest_path: str, content: bytes
    ) -> None: ...

    async def create_session(
        self, sandbox: CreatedSandbox, *, session_id: str
    ) -> None: ...

    async def exec_async(
        self,
        sandbox: CreatedSandbox,
        *,
        daytona_session_id: str,
        command: list[str] | str,
    ) -> str:
        """Returns the cmd_id."""
        ...

    async def stream_logs(
        self,
        sandbox: CreatedSandbox,
        *,
        daytona_session_id: str,
        cmd_id: str,
        on_stdout: Callable[[str], Awaitable[None]],
        on_stderr: Callable[[str], Awaitable[None]],
    ) -> None: ...

    async def exec_sync(
        self, sandbox: CreatedSandbox, *, command: str
    ) -> tuple[int, str]:
        """Run a one-off command, wait for completion, return (exit_code, output)."""
        ...

    async def download_file(
        self, sandbox: CreatedSandbox, *, src_path: str
    ) -> bytes: ...

    async def delete_sandbox(self, sandbox: CreatedSandbox) -> None: ...


class RealDaytonaClient:
    """Production implementation backed by the Daytona Python SDK."""

    def __init__(self, *, api_key: str) -> None:
        self._daytona = AsyncDaytona(DaytonaConfig(api_key=api_key))

    async def create_sandbox(
        self, *, env_vars: dict[str, str], auto_stop_minutes: int
    ) -> CreatedSandbox:
        sbx = await self._daytona.create(
            CreateSandboxFromSnapshotParams(
                env_vars=env_vars,
                auto_stop_interval=auto_stop_minutes,
            )
        )
        return CreatedSandbox(id=sbx.id, _sdk_handle=sbx)

    async def upload_file(
        self, sandbox: CreatedSandbox, *, dest_path: str, content: bytes
    ) -> None:
        await sandbox._sdk_handle.fs.upload_file(content, dest_path)

    async def create_session(
        self, sandbox: CreatedSandbox, *, session_id: str
    ) -> None:
        await sandbox._sdk_handle.process.create_session(session_id)

    async def exec_async(
        self,
        sandbox: CreatedSandbox,
        *,
        daytona_session_id: str,
        command: list[str] | str,
    ) -> str:
        if isinstance(command, list):
            command_str = " ".join(command)
        else:
            command_str = command
        result = await sandbox._sdk_handle.process.execute_session_command(
            daytona_session_id,
            SessionExecuteRequest(command=command_str, run_async=True),
        )
        return result.cmd_id

    async def stream_logs(
        self,
        sandbox: CreatedSandbox,
        *,
        daytona_session_id: str,
        cmd_id: str,
        on_stdout: Callable[[str], Awaitable[None]],
        on_stderr: Callable[[str], Awaitable[None]],
        poll_interval: float = 0.5,
    ) -> None:
        """Stream a session command's logs by polling the REST API.

        The Daytona SDK's WebSocket-based streaming
        (`get_session_command_logs_async`) currently raises
        "WebSocket error: no close frame received or sent" on connect, so we
        fall back to polling the cumulative log snapshot and emitting deltas.

        Returns when the command exits.
        """
        process = sandbox._sdk_handle.process
        last_stdout_len = 0
        last_stderr_len = 0
        polls = 0
        last_progress_poll = 0
        consecutive_errors = 0
        max_consecutive_errors = 10  # ~5s of failures at 500ms poll
        logger.info(
            "stream_logs: cmd=%s polling started (interval=%ss)",
            cmd_id,
            poll_interval,
        )
        while True:
            polls += 1
            try:
                logs = await process.get_session_command_logs(
                    daytona_session_id, cmd_id
                )
                consecutive_errors = 0
            except Exception:
                consecutive_errors += 1
                logger.warning(
                    "stream_logs: log fetch failed cmd=%s (errors=%d/%d)",
                    cmd_id,
                    consecutive_errors,
                    max_consecutive_errors,
                )
                if consecutive_errors >= max_consecutive_errors:
                    # Sandbox almost certainly gone (deleted on session
                    # close, auto-stopped, or crashed). Stop polling so
                    # the caller can finalize the SSE stream instead of
                    # spamming Daytona forever.
                    raise
                await asyncio.sleep(poll_interval)
                continue

            stdout = logs.stdout or ""
            stderr = logs.stderr or ""
            if len(stdout) > last_stdout_len:
                await on_stdout(stdout[last_stdout_len:])
                last_stdout_len = len(stdout)
            if len(stderr) > last_stderr_len:
                await on_stderr(stderr[last_stderr_len:])
                last_stderr_len = len(stderr)

            try:
                cmd = await process.get_session_command(
                    daytona_session_id, cmd_id
                )
                consecutive_errors = 0
            except Exception:
                consecutive_errors += 1
                logger.warning(
                    "stream_logs: status fetch failed cmd=%s (errors=%d/%d)",
                    cmd_id,
                    consecutive_errors,
                    max_consecutive_errors,
                )
                if consecutive_errors >= max_consecutive_errors:
                    raise
                await asyncio.sleep(poll_interval)
                continue

            # Heartbeat every ~10s so a silent claude (no output, still
            # running) is distinguishable from a wedged poll loop.
            if polls - last_progress_poll >= 20:
                logger.info(
                    "stream_logs: cmd=%s polling poll#%d stdout=%dB stderr=%dB exit=%s",
                    cmd_id,
                    polls,
                    last_stdout_len,
                    last_stderr_len,
                    cmd.exit_code,
                )
                last_progress_poll = polls

            if cmd.exit_code is not None:
                # Final flush in case logs grew between the two calls above.
                try:
                    final = await process.get_session_command_logs(
                        daytona_session_id, cmd_id
                    )
                    fstdout = final.stdout or ""
                    fstderr = final.stderr or ""
                    if len(fstdout) > last_stdout_len:
                        await on_stdout(fstdout[last_stdout_len:])
                    if len(fstderr) > last_stderr_len:
                        await on_stderr(fstderr[last_stderr_len:])
                except Exception:
                    logger.exception(
                        "stream_logs: final flush failed cmd=%s", cmd_id
                    )
                logger.info(
                    "stream_logs: cmd=%s exit_code=%s", cmd_id, cmd.exit_code
                )
                return

            await asyncio.sleep(poll_interval)

    async def exec_sync(
        self, sandbox: CreatedSandbox, *, command: str
    ) -> tuple[int, str]:
        result = await sandbox._sdk_handle.process.exec(command)
        return result.exit_code, result.result

    async def download_file(
        self, sandbox: CreatedSandbox, *, src_path: str
    ) -> bytes:
        return await sandbox._sdk_handle.fs.download_file(src_path)

    async def delete_sandbox(self, sandbox: CreatedSandbox) -> None:
        await self._daytona.delete(sandbox._sdk_handle)
