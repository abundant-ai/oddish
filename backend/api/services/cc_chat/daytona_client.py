from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Protocol

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
        command: list[str],
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
        command: list[str],
    ) -> str:
        result = await sandbox._sdk_handle.process.execute_session_command(
            daytona_session_id,
            SessionExecuteRequest(command=" ".join(command), run_async=True),
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
    ) -> None:
        await sandbox._sdk_handle.process.get_session_command_logs_async(
            daytona_session_id,
            cmd_id,
            on_stdout,
            on_stderr,
        )

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
