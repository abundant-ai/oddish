from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

import pytest

from api.services.cc_chat.daytona_client import CreatedSandbox


@dataclass
class FakeSandbox:
    id: str
    files: dict[str, bytes] = field(default_factory=dict)
    sessions: list[str] = field(default_factory=list)
    deleted: bool = False


class FakeDaytonaClient:
    """Records calls; lets tests assert on the resulting in-memory state."""

    def __init__(self) -> None:
        self.created: list[FakeSandbox] = []
        self.execs: list[dict] = []
        # Tests set this to control what stream_logs emits.
        self.canned_stdout_chunks: list[str] = []
        self.canned_stderr_chunks: list[str] = []

    async def create_sandbox(
        self, *, env_vars: dict[str, str], auto_stop_minutes: int
    ) -> CreatedSandbox:
        sbx = FakeSandbox(id=f"sbx-{len(self.created)}")
        self.created.append(sbx)
        sbx.env_vars = env_vars
        sbx.auto_stop_minutes = auto_stop_minutes
        return CreatedSandbox(id=sbx.id, _sdk_handle=sbx)

    async def upload_file(
        self, sandbox: CreatedSandbox, *, dest_path: str, content: bytes
    ) -> None:
        sandbox._sdk_handle.files[dest_path] = content

    async def create_session(
        self, sandbox: CreatedSandbox, *, session_id: str
    ) -> None:
        sandbox._sdk_handle.sessions.append(session_id)

    async def exec_async(
        self,
        sandbox: CreatedSandbox,
        *,
        daytona_session_id: str,
        command: list[str],
    ) -> str:
        cmd_id = f"cmd-{len(self.execs)}"
        self.execs.append(
            {
                "sandbox_id": sandbox.id,
                "session": daytona_session_id,
                "command": command,
                "cmd_id": cmd_id,
            }
        )
        return cmd_id

    async def stream_logs(
        self,
        sandbox: CreatedSandbox,
        *,
        daytona_session_id: str,
        cmd_id: str,
        on_stdout: Callable[[str], Awaitable[None]],
        on_stderr: Callable[[str], Awaitable[None]],
    ) -> None:
        for chunk in self.canned_stdout_chunks:
            await on_stdout(chunk)
        for chunk in self.canned_stderr_chunks:
            await on_stderr(chunk)

    async def delete_sandbox(self, sandbox: CreatedSandbox) -> None:
        sandbox._sdk_handle.deleted = True


@pytest.fixture
def fake_daytona() -> FakeDaytonaClient:
    return FakeDaytonaClient()
