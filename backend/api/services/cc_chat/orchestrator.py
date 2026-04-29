from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import AsyncIterator

from api.services.cc_chat.claude_md import render_claude_md
from api.services.cc_chat.daytona_client import CreatedSandbox, DaytonaClient
from api.services.cc_chat.file_store import ExperimentFileStore
from api.services.cc_chat.sessions import SessionRegistry, SessionState


_DAYTONA_SESSION_NAME = "cc"
_WORKSPACE_ROOT = "/workspace"


class SessionNotFound(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_session_id() -> str:
    return f"cc-{secrets.token_urlsafe(12)}"


class CCChatOrchestrator:
    def __init__(
        self,
        *,
        daytona: DaytonaClient,
        file_store: ExperimentFileStore,
        anthropic_api_key: str,
        auto_stop_minutes: int = 30,
    ) -> None:
        self._daytona = daytona
        self._file_store = file_store
        self._anthropic_api_key = anthropic_api_key
        self._auto_stop_minutes = auto_stop_minutes
        self._sessions = SessionRegistry()
        self._sandbox_handles: dict[str, CreatedSandbox] = {}

    async def start(self, *, experiment_id: str, org_id: str) -> str:
        sandbox = await self._daytona.create_sandbox(
            env_vars={"ANTHROPIC_API_KEY": self._anthropic_api_key},
            auto_stop_minutes=self._auto_stop_minutes,
        )
        try:
            await self._daytona.create_session(
                sandbox, session_id=_DAYTONA_SESSION_NAME
            )
            await self._daytona.exec_async(
                sandbox,
                daytona_session_id=_DAYTONA_SESSION_NAME,
                command=[
                    "npm", "install", "-g", "@anthropic-ai/claude-code",
                ],
            )

            trial_ids: list[str] = []
            async for rel, content in self._file_store.iter_files(
                experiment_id
            ):
                trial_id = PurePosixPath(rel).parts[0]
                if trial_id not in trial_ids:
                    trial_ids.append(trial_id)
                dest = (
                    f"{_WORKSPACE_ROOT}/jobs/{experiment_id}/{rel}"
                )
                await self._daytona.upload_file(
                    sandbox, dest_path=dest, content=content
                )

            claude_md = render_claude_md(
                experiment_id=experiment_id, trial_ids=trial_ids
            )
            await self._daytona.upload_file(
                sandbox,
                dest_path=f"{_WORKSPACE_ROOT}/CLAUDE.md",
                content=claude_md.encode("utf-8"),
            )
        except Exception:
            await self._daytona.delete_sandbox(sandbox)
            raise

        session_id = _new_session_id()
        now = _now()
        self._sessions.put(
            SessionState(
                session_id=session_id,
                experiment_id=experiment_id,
                org_id=org_id,
                sandbox_id=sandbox.id,
                daytona_session_id=_DAYTONA_SESSION_NAME,
                created_at=now,
                last_activity=now,
                claude_session_id=None,
            )
        )
        self._sandbox_handles[session_id] = sandbox
        return session_id

    async def send(
        self, *, session_id: str, content: str
    ) -> AsyncIterator[dict]:
        state = self._sessions.get(session_id)
        if state is None or state.broken:
            raise SessionNotFound(session_id)
        sandbox = self._sandbox_handles[session_id]

        cmd: list[str] = [
            "claude",
            "--print",
            "--output-format=stream-json",
        ]
        if state.claude_session_id:
            cmd += ["--resume", state.claude_session_id]
        cmd += ["--", content]

        cmd_id = await self._daytona.exec_async(
            sandbox,
            daytona_session_id=state.daytona_session_id,
            command=cmd,
        )

        queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()

        async def on_stdout(chunk: str) -> None:
            await queue.put(("stdout", chunk))

        async def on_stderr(chunk: str) -> None:
            await queue.put(("stderr", chunk))

        stream_task = asyncio.create_task(
            self._daytona.stream_logs(
                sandbox,
                daytona_session_id=state.daytona_session_id,
                cmd_id=cmd_id,
                on_stdout=on_stdout,
                on_stderr=on_stderr,
            )
        )

        async def closer() -> None:
            try:
                await stream_task
            finally:
                await queue.put(None)

        closer_task = asyncio.create_task(closer())

        leftover = ""
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                kind, chunk = item
                if kind == "stderr":
                    yield {
                        "type": "_stderr",
                        "text": chunk,
                    }
                    continue
                leftover += chunk
                while "\n" in leftover:
                    line, leftover = leftover.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        yield {"type": "_invalid_json", "raw": line}
                        continue
                    if (
                        event.get("type") == "system"
                        and event.get("subtype") == "init"
                        and "session_id" in event
                    ):
                        state.claude_session_id = event["session_id"]
                    state.last_activity = _now()
                    yield event
            if leftover.strip():
                try:
                    yield json.loads(leftover.strip())
                except json.JSONDecodeError:
                    yield {"type": "_invalid_json", "raw": leftover.strip()}
        finally:
            await closer_task

    async def close(self, *, session_id: str) -> None:
        state = self._sessions.pop(session_id)
        if state is None:
            return
        sandbox = self._sandbox_handles.pop(session_id, None)
        if sandbox is not None:
            await self._daytona.delete_sandbox(sandbox)
