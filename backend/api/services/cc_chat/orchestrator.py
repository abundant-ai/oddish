from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from importlib import resources
from typing import Callable, Literal

from daytona import DaytonaNotFoundError
from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

from models import (
    APIKeyModel,
    ChatSession,
    ChatStatus,
    generate_id,
)
from oddish.db.models import utcnow as _utcnow
from oddish.core.api_keys import mint_internal_read_key
from api.services.cc_chat.archive import archive_native_session, restore_native_session
from api.services.cc_chat.claude_md import (
    render_experiment_claude_md,
    render_global_claude_md,
    render_task_chat_claude_md,
    render_task_probes_claude_md,
)
from api.services.cc_chat.daytona_client import CreatedSandbox, DaytonaClient
from api.services.cc_chat.events import append_event, prune_events
from api.services.cc_chat.provisioner import Provisioner, delete_sandbox_quietly
from api.services.cc_chat.transcript_buffer import SessionTranscriptBuffer
from api.services.cc_chat.turns import close_turn, open_turn

# The internal READ key minted for a chat sandbox expires after this many
# minutes. This expiry is the eviction backstop: a sandbox that escapes
# close() still loses backend access once the key lapses.
GLOBAL_QUERY_KEY_TTL_MINUTES = 45

# The oddish-query CLI is uploaded here and made executable.
QUERY_CLI_DEST = "oddish-query"

log = logging.getLogger("oddish.cc_chat.orchestrator")

WORKSPACE_ROOT = "/home/daytona/workspace"
DAYTONA_SESSION_ID = "cc"


def _now() -> datetime:
    # tz-aware, matching the DateTime(timezone=True) columns and the rest of the repo
    return _utcnow()


class SessionNotFound(Exception):
    pass


class ResumeUnavailable(Exception):
    pass


class ChatOrchestrator:
    """Phase-1 chat orchestrator. Runs Claude Code in a Daytona sandbox and
    streams stream-json events, persisting every event durably to the
    chat_session_events log and tracking each message as a chat_turns row.
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
        public_api_base_url: str = "",
        blob_store=None,
    ) -> None:
        self._daytona = daytona
        self._runtime = runtime
        self._buffer = transcript_buffer
        self._anthropic_api_key = anthropic_api_key
        self._auto_stop = chat_auto_stop_minutes
        self._auto_delete = chat_auto_delete_minutes
        self._public_api_base_url = public_api_base_url
        self._blob = blob_store
        self._sandboxes: dict[str, CreatedSandbox] = {}

    async def _resolve_scope_inputs(
        self,
        *,
        scope_kind: str,
        scope_id: str,
        org_id: str | None,
        db_session_factory: Callable[[], object],
    ) -> str:
        """Resolve the CLAUDE.md text for the given scope."""
        if scope_kind == "experiment":
            return render_experiment_claude_md(experiment_id=scope_id)
        if scope_kind == "task":
            return render_task_chat_claude_md(task_name=scope_id)
        if scope_kind == "task_probes":
            return render_task_probes_claude_md(task_name=scope_id)
        return render_global_claude_md(org_id=org_id or scope_id)

    async def _provision_sandbox(
        self,
        *,
        session_id: str,
        claude_md: str,
        extra_env: dict[str, str] | None = None,
    ) -> CreatedSandbox:
        """Provision a fresh sandbox for a chat session: create the Daytona
        sandbox, install the runtime, upload CLAUDE.md and the oddish-query CLI.
        On any failure the partially-created sandbox is deleted and the error
        re-raised. Shared by start() and resume() so their provisioning never
        drifts."""
        env_vars = {"ANTHROPIC_API_KEY": self._anthropic_api_key}
        if extra_env:
            env_vars.update(extra_env)

        sandbox = await Provisioner(client=self._daytona).create(
            env_vars=env_vars,
            auto_stop_minutes=self._auto_stop,
            auto_delete_minutes=self._auto_delete,
            labels={"app": "cc_chat", "session_id": session_id},
            daytona_session_id=DAYTONA_SESSION_ID,
        )
        try:
            await self._runtime.install(self._daytona, sandbox)
            await self._daytona.upload_file(
                sandbox,
                dest_path=f"{WORKSPACE_ROOT}/CLAUDE.md",
                content=claude_md.encode("utf-8"),
            )
            await self._upload_query_cli(sandbox)
        except Exception:
            await delete_sandbox_quietly(self._daytona, sandbox)
            raise
        return sandbox

    async def _upload_query_cli(self, sandbox: CreatedSandbox) -> None:
        """Upload the Node oddish-query CLI and make it executable."""
        cli_bytes = resources.files("oddish").joinpath("assets/oddish-query").read_bytes()
        dest = f"{WORKSPACE_ROOT}/{QUERY_CLI_DEST}"
        await self._daytona.upload_file(sandbox, dest_path=dest, content=cli_bytes)
        await self._daytona.exec_sync(sandbox, command=f"chmod +x {dest}")

    async def _mint_query_key(
        self, *, org_id: str, session_id: str, db_session_factory: Callable[[], object]
    ) -> tuple[str, str]:
        """Mint a READ-scoped internal API key for the sandbox. Returns (key_id, raw_key)."""
        async with self._db(db_session_factory) as db:
            return await mint_internal_read_key(
                db,
                org_id=org_id,
                name=f"cc-chat:{session_id}",
                ttl_minutes=GLOBAL_QUERY_KEY_TTL_MINUTES,
            )

    async def start(
        self,
        *,
        org_id: str,
        user_id: str | None,
        scope_kind: Literal["experiment", "task_probes", "task", "global"],
        scope_id: str,
        db_session_factory: Callable[[], object],
    ) -> str:
        if not self._public_api_base_url:
            raise RuntimeError("ODDISH_PUBLIC_API_BASE_URL must be set for chat sessions")

        session_id = generate_id()

        claude_md = await self._resolve_scope_inputs(
            scope_kind=scope_kind,
            scope_id=scope_id,
            org_id=org_id,
            db_session_factory=db_session_factory,
        )

        query_api_key_id, raw_key = await self._mint_query_key(
            org_id=org_id, session_id=session_id, db_session_factory=db_session_factory,
        )
        extra_env = {
            "ODDISH_API_KEY": raw_key,
            "ODDISH_API_BASE_URL": self._public_api_base_url,
        }

        async with self._db(db_session_factory) as db:
            db.add(
                ChatSession(
                    id=session_id,
                    org_id=org_id,
                    user_id=user_id,
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    status=ChatStatus.provisioning.value,
                    query_api_key_id=query_api_key_id,
                    created_at=_now(),
                    last_activity=_now(),
                )
            )
            await db.commit()

        try:
            sandbox = await self._provision_sandbox(
                session_id=session_id,
                claude_md=claude_md,
                extra_env=extra_env,
            )
        except Exception:
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

    async def resume(
        self,
        *,
        session_id: str,
        db_session_factory: Callable[[], object],
    ) -> None:
        # Already attached in-process → nothing to do.
        if self._sandboxes.get(session_id) is not None:
            return

        async with self._db(db_session_factory) as db:
            row = await db.get(ChatSession, session_id)
            if row is None:
                raise SessionNotFound(session_id)
            scope_kind = row.scope_kind
            scope_id = row.scope_id
            org_id = row.org_id
            claude_session_id = row.claude_session_id
            prior_query_api_key_id = row.query_api_key_id

        if not self._public_api_base_url:
            raise RuntimeError("ODDISH_PUBLIC_API_BASE_URL must be set for chat sessions")

        claude_md = await self._resolve_scope_inputs(
            scope_kind=scope_kind,
            scope_id=scope_id,
            org_id=org_id,
            db_session_factory=db_session_factory,
        )

        # Hard-delete the prior key before minting a fresh one to avoid orphaning
        # it (broken sessions are never revoked by close()).
        if prior_query_api_key_id is not None:
            try:
                async with self._db(db_session_factory) as db:
                    prior_key = await db.get(APIKeyModel, prior_query_api_key_id)
                    if prior_key is not None:
                        await db.delete(prior_key)
                        await db.commit()
            except Exception:
                log.exception(
                    "prior query api-key delete failed: session=%s key=%s",
                    session_id,
                    prior_query_api_key_id,
                )

        new_query_api_key_id, raw_key = await self._mint_query_key(
            org_id=org_id, session_id=session_id, db_session_factory=db_session_factory,
        )
        extra_env = {
            "ODDISH_API_KEY": raw_key,
            "ODDISH_API_BASE_URL": self._public_api_base_url,
        }

        sandbox = await self._provision_sandbox(
            session_id=session_id,
            claude_md=claude_md,
            extra_env=extra_env,
        )
        try:
            if claude_session_id:
                restored = await restore_native_session(
                    self._daytona, sandbox, blob=self._blob,
                    session_id=session_id, claude_session_id=claude_session_id,
                )
            else:
                restored = False
            if not restored:
                raise ResumeUnavailable(session_id)
        except Exception:
            await delete_sandbox_quietly(self._daytona, sandbox)
            raise

        self._sandboxes[session_id] = sandbox
        async with self._db(db_session_factory) as db:
            row = await db.get(ChatSession, session_id)
            row.sandbox_id = sandbox.id
            row.status = ChatStatus.active.value
            row.error = None
            row.closed_at = None
            row.last_activity = _now()
            row.query_api_key_id = new_query_api_key_id
            await db.commit()

    async def _reconnect_sandbox(
        self, *, session_id: str, db_session_factory: Callable[[], object]
    ) -> CreatedSandbox | None:
        """Return a live sandbox handle for the session, rehydrating it when
        this container didn't create it.

        The API runs across many autoscaled containers with no session
        affinity, so the container handling a message is usually not the one
        that ran start(); ``self._sandboxes`` is per-process. When the handle
        isn't cached locally we reconnect to the existing Daytona sandbox by
        its persisted id so any container can serve the session. Returns None
        when the session has no sandbox or it no longer exists.
        """
        cached = self._sandboxes.get(session_id)
        if cached is not None:
            return cached
        async with self._db(db_session_factory) as db:
            row = await db.get(ChatSession, session_id)
            sandbox_id = row.sandbox_id if row is not None else None
        if not sandbox_id:
            return None
        try:
            sandbox = await self._daytona.connect_sandbox(sandbox_id=sandbox_id)
        except DaytonaNotFoundError:
            return None
        self._sandboxes[session_id] = sandbox
        return sandbox

    async def _heal_sandbox(
        self, *, session_id: str, db_session_factory: Callable[[], object]
    ) -> CreatedSandbox | None:
        """Re-provision + restore a session whose ephemeral sandbox was evicted.

        Idle chats lose their sandbox to Daytona's auto-stop, so a returning
        user would otherwise hit ``session_not_found`` mid-conversation. When the
        session has an archive (a ``claude_session_id`` written by a prior turn),
        resume() rebuilds the sandbox from it and the next message just works.
        Returns the live handle, or None when there's nothing to restore yet.
        """
        async with self._db(db_session_factory) as db:
            row = await db.get(ChatSession, session_id)
            recoverable = row is not None and row.claude_session_id is not None
        if not recoverable:
            return None
        try:
            await self.resume(session_id=session_id, db_session_factory=db_session_factory)
        except (ResumeUnavailable, SessionNotFound):
            return None
        return self._sandboxes.get(session_id)

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

        sandbox = await self._reconnect_sandbox(
            session_id=session_id, db_session_factory=db_session_factory
        )
        if sandbox is None:
            # Sandbox evicted (idle auto-stop) — transparently rebuild it.
            sandbox = await self._heal_sandbox(
                session_id=session_id, db_session_factory=db_session_factory
            )
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

        if self._blob is not None and claude_session_id is not None and sandbox is not None:
            try:
                await archive_native_session(
                    self._daytona, sandbox, blob=self._blob,
                    session_id=session_id, claude_session_id=claude_session_id,
                )
            except Exception:
                log.exception("post-turn archival failed: %s", session_id)

    async def close(
        self,
        *,
        session_id: str,
        db_session_factory: Callable[[], object],
    ) -> None:
        sandbox = self._sandboxes.pop(session_id, None)
        if sandbox is None:
            # close may land on a different container than start(); reconnect
            # so the remote sandbox is actually deleted, not leaked until its
            # ephemeral auto-stop fires.
            sandbox = await self._reconnect_sandbox(
                session_id=session_id, db_session_factory=db_session_factory
            )
            self._sandboxes.pop(session_id, None)

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

        query_api_key_id: str | None = None
        async with self._db(db_session_factory) as db:
            row = await db.get(ChatSession, session_id)
            if row is not None:
                query_api_key_id = row.query_api_key_id
                if row.status != ChatStatus.broken.value:
                    row.status = ChatStatus.closed.value
                row.closed_at = _now()
                await db.commit()

        # Revoke the internal oddish-query key. Best-effort: the 45-min expiry
        # is the backstop if this delete fails.
        if query_api_key_id is not None:
            try:
                async with self._db(db_session_factory) as db:
                    api_key = await db.get(APIKeyModel, query_api_key_id)
                    if api_key is not None:
                        await db.delete(api_key)
                        await db.commit()
            except Exception:
                log.exception(
                    "query api-key revoke failed: session=%s key=%s",
                    session_id,
                    query_api_key_id,
                )

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
