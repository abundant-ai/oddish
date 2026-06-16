from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Callable, Literal

from daytona import DaytonaNotFoundError
from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

from models import ChatSession, ChatStatus, generate_id
from oddish.db.models import utcnow as _utcnow
from api.services.cc_chat.claude_md import (
    render_experiment_claude_md,
    render_task_chat_claude_md,
    render_task_probes_claude_md,
)
from api.services.cc_chat.daytona_client import CreatedSandbox, DaytonaClient
from api.services.cc_chat.events import append_event, prune_events
from api.services.cc_chat.file_loader import upload_files
from api.services.cc_chat.task_files import collect_task_version_files
from api.services.cc_chat.provisioner import Provisioner, delete_sandbox_quietly
from api.services.cc_chat.transcript_buffer import SessionTranscriptBuffer
from api.services.cc_chat.turns import close_turn, open_turn

log = logging.getLogger("oddish.cc_chat.orchestrator")

WORKSPACE_ROOT = "/home/daytona/workspace"
DAYTONA_SESSION_ID = "cc"


def _now() -> datetime:
    # tz-aware, matching the DateTime(timezone=True) columns and the rest of the repo
    return _utcnow()


class SessionNotFound(Exception):
    pass


class ChatOrchestrator:
    """Phase-1 chat orchestrator. Runs Claude Code in a Daytona sandbox and
    streams stream-json events, persisting every event durably to the
    chat_session_events log and tracking each message as a chat_turns row.

    Phase-1 simplifications vs. the agent-sandbox-service original:
    - start() does NOT sync trial/probe artifacts into the sandbox (deferred to
      Phase 2 via oddish.core); it only renders CLAUDE.md + uploads it.
    - No OddishClient / ProbeRun dependency.
    """

    def __init__(
        self,
        *,
        daytona: DaytonaClient,
        runtime,
        transcript_buffer: SessionTranscriptBuffer,
        anthropic_api_key: str,
        chat_auto_stop_minutes: int = 30,
        chat_auto_delete_minutes: int = 60,
        blob_store=None,
    ) -> None:
        self._daytona = daytona
        self._runtime = runtime
        self._buffer = transcript_buffer
        self._anthropic_api_key = anthropic_api_key
        self._auto_stop = chat_auto_stop_minutes
        self._auto_delete = chat_auto_delete_minutes
        self._blob = blob_store
        self._sandboxes: dict[str, CreatedSandbox] = {}

    async def start(
        self,
        *,
        org_id: str,
        user_id: str | None,
        scope_kind: Literal["experiment", "task_probes", "task"],
        scope_id: str,
        db_session_factory: Callable[[], object],
    ) -> str:
        files: list[tuple[str, bytes]] = []
        if scope_kind == "experiment":
            claude_md = render_experiment_claude_md(experiment_id=scope_id, trial_ids=[])
        elif scope_kind == "task":
            if self._blob is None:
                raise RuntimeError("blob_store is required for task-scope chat sessions")
            async with self._db(db_session_factory) as db:
                current_version, version_trials, files, truncated = await collect_task_version_files(
                    db, self._blob, task_id=scope_id, org_id=org_id,
                )
            if truncated:
                log.warning("cc_chat task-scope upload truncated at byte cap: task=%s", scope_id)
            claude_md = render_task_chat_claude_md(
                task_name=scope_id,
                current_version=current_version,
                version_trials=version_trials,
            )
        else:  # task_probes
            claude_md = render_task_probes_claude_md(task_name=scope_id, trial_ids=[])

        session_id = generate_id()
        async with self._db(db_session_factory) as db:
            db.add(
                ChatSession(
                    id=session_id,
                    org_id=org_id,
                    user_id=user_id,
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    status=ChatStatus.provisioning.value,
                    created_at=_now(),
                    last_activity=_now(),
                )
            )
            await db.commit()

        sandbox = await Provisioner(client=self._daytona).create(
            env_vars={"ANTHROPIC_API_KEY": self._anthropic_api_key},
            auto_stop_minutes=self._auto_stop,
            auto_delete_minutes=self._auto_delete,
            labels={"app": "cc_chat", "session_id": session_id},
            daytona_session_id=DAYTONA_SESSION_ID,
        )
        try:
            await self._runtime.install(self._daytona, sandbox)
            if files:
                await upload_files(
                    self._daytona, sandbox, files=files, workspace_root=WORKSPACE_ROOT,
                )
            await self._daytona.upload_file(
                sandbox,
                dest_path=f"{WORKSPACE_ROOT}/CLAUDE.md",
                content=claude_md.encode("utf-8"),
            )
        except Exception:
            await delete_sandbox_quietly(self._daytona, sandbox)
            async with self._db(db_session_factory) as db:
                row = await db.get(ChatSession, session_id)
                if row is not None:
                    row.status = ChatStatus.broken.value
                    row.error = "provisioning failed"
                    row.closed_at = _now()
                    await db.commit()
            raise

        self._sandboxes[session_id] = sandbox
        async with self._db(db_session_factory) as db:
            row = await db.get(ChatSession, session_id)
            row.sandbox_id = sandbox.id
            row.status = ChatStatus.active.value
            row.last_activity = _now()
            await db.commit()
        return session_id

    async def send(
        self,
        *,
        session_id: str,
        content: str,
        db_session_factory: Callable[[], object],
    ):
        async with self._db(db_session_factory) as db:
            row = await db.get(ChatSession, session_id)
            if row is None or row.status != ChatStatus.active.value:
                raise SessionNotFound(session_id)
            claude_session_id = row.claude_session_id

        sandbox = self._sandboxes.get(session_id)
        if sandbox is None:
            raise SessionNotFound(session_id)

        async with self._db(db_session_factory) as db:
            turn = await open_turn(db, session_id=session_id, user_message=content)
            await db.commit()
            turn_id = turn.id

        turn_status = "done"
        turn_error: str | None = None
        try:
            async for event in self._runtime.stream_chat(
                self._daytona,
                sandbox,
                content=content,
                claude_session_id=claude_session_id,
                daytona_session_id=DAYTONA_SESSION_ID,
            ):
                if (
                    event.get("type") == "system"
                    and event.get("subtype") == "init"
                    and "session_id" in event
                ):
                    claude_session_id = event["session_id"]
                    async with self._db(db_session_factory) as db:
                        r = await db.get(ChatSession, session_id)
                        if r is not None:
                            r.claude_session_id = claude_session_id
                            await db.commit()

                # write-through cache + durable append, then yield
                self._buffer.append(session_id, event)
                async with self._db(db_session_factory) as db:
                    await append_event(db, session_id=session_id, event=event)
                    await db.commit()
                yield event
        except DaytonaNotFoundError:
            turn_status, turn_error = "failed", "sandbox no longer exists"
            self._sandboxes.pop(session_id, None)
            async with self._db(db_session_factory) as db:
                row = await db.get(ChatSession, session_id)
                if row is not None:
                    row.status = ChatStatus.broken.value
                    row.error = turn_error
                    row.closed_at = _now()
                    await db.commit()
            return
        except Exception as exc:
            turn_status, turn_error = "failed", str(exc)
            raise
        except BaseException:
            # client disconnect / cancellation — close the turn so it does not
            # leak as 'running' and wedge the one-running-turn invariant.
            turn_status, turn_error = "canceled", "client disconnected"
            raise
        finally:
            async with self._db(db_session_factory) as db:
                await close_turn(db, turn_id=turn_id, status=turn_status, error=turn_error)
                await db.commit()

        async with self._db(db_session_factory) as db:
            r = await db.get(ChatSession, session_id)
            if r is not None:
                r.last_activity = _now()
                await db.commit()

    async def close(
        self,
        *,
        session_id: str,
        db_session_factory: Callable[[], object],
    ) -> None:
        sandbox = self._sandboxes.pop(session_id, None)

        # Flush the cold archive (object storage) before pruning the hot log.
        if self._blob is not None:
            blob_bytes = self._buffer.to_jsonl(session_id)
            if blob_bytes:
                try:
                    await self._blob.upload_bytes(
                        blob_bytes,
                        f"chat-sessions/{session_id}/transcript.jsonl",
                        content_type="application/x-ndjson",
                    )
                except Exception:
                    log.exception("transcript flush failed: %s", session_id)
        self._buffer.drop(session_id)

        # Prune the durable event log only after the cold archive is flushed.
        async with self._db(db_session_factory) as db:
            await prune_events(db, session_id=session_id)
            await db.commit()

        if sandbox is not None:
            await delete_sandbox_quietly(self._daytona, sandbox)

        async with self._db(db_session_factory) as db:
            row = await db.get(ChatSession, session_id)
            if row is not None:
                if row.status != ChatStatus.broken.value:
                    row.status = ChatStatus.closed.value
                row.closed_at = _now()
                await db.commit()

    async def export_skills(self, *, session_id: str) -> bytes:
        """Tar /home/daytona/workspace/.claude/skills/ from the sandbox and return bytes."""
        sandbox = self._sandboxes.get(session_id)
        if sandbox is None:
            raise SessionNotFound(session_id)
        archive_path = "/tmp/cc-chat-skills.tar.gz"
        skills_dir = f"{WORKSPACE_ROOT}/.claude/skills"
        await self._daytona.exec_sync(
            sandbox,
            command=(
                f"mkdir -p {skills_dir} && "
                f"tar -czf {archive_path} -C {skills_dir} . || true"
            ),
        )
        return await self._daytona.download_file(sandbox, src_path=archive_path)

    @staticmethod
    def _db(factory):
        @asynccontextmanager
        async def _open():
            sess = factory()
            if isinstance(sess, _AsyncSession):
                yield sess
            else:
                async with sess as s:
                    yield s

        return _open()
