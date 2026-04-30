from __future__ import annotations

import asyncio
import json
import logging
import secrets
import shlex
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import AsyncIterator

from api.services.cc_chat.claude_md import render_claude_md
from api.services.cc_chat.daytona_client import CreatedSandbox, DaytonaClient
from api.services.cc_chat.file_store import ExperimentFileStore
from api.services.cc_chat.sessions import SessionRegistry, SessionState


logger = logging.getLogger("cc_chat.orchestrator")

_DAYTONA_SESSION_NAME = "cc"
_WORKSPACE_ROOT = "/home/daytona/workspace"
_NPM_PREFIX = "/home/daytona/.npm-global"
_CLAUDE_BIN = f"{_NPM_PREFIX}/bin/claude"


class SessionNotFound(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_session_id() -> str:
    return f"cc-{secrets.token_urlsafe(12)}"


# Path components / suffixes that pull a lot of bytes for little chat value.
# Tuned against abundant trial dirs: agent/sessions and agent/skills together
# are ~12MB, and the sqlite state DBs are opaque binaries the model won't read.
_SKIP_PATH_PARTS = frozenset(
    {"sessions", "skills", "backups", "node_modules", "__pycache__"}
)
_SKIP_SUFFIXES = (".sqlite", ".sqlite-journal", ".db", ".pyc", ".so")


def _should_skip(rel_path: str) -> bool:
    parts = rel_path.split("/")
    if any(part in _SKIP_PATH_PARTS for part in parts):
        return True
    if rel_path.endswith(_SKIP_SUFFIXES):
        return True
    return False


class CCChatOrchestrator:
    def __init__(
        self,
        *,
        daytona: DaytonaClient,
        file_store: ExperimentFileStore,
        anthropic_api_key: str,
        auto_stop_minutes: int = 30,
        skills_dir: Path | None = None,
        idle_timeout_minutes: int = 35,
        sweep_interval_seconds: int = 60,
    ) -> None:
        self._daytona = daytona
        self._file_store = file_store
        self._anthropic_api_key = anthropic_api_key
        self._auto_stop_minutes = auto_stop_minutes
        self._skills_dir = skills_dir
        self._sessions = SessionRegistry()
        self._sandbox_handles: dict[str, CreatedSandbox] = {}
        self._idle_timeout = timedelta(minutes=idle_timeout_minutes)
        self._sweep_interval = sweep_interval_seconds
        self._sweeper_task: asyncio.Task[None] | None = None

    async def start(self, *, experiment_id: str, org_id: str) -> str:
        sandbox = await self._daytona.create_sandbox(
            env_vars={"ANTHROPIC_API_KEY": self._anthropic_api_key},
            auto_stop_minutes=self._auto_stop_minutes,
        )
        try:
            await self._daytona.create_session(
                sandbox, session_id=_DAYTONA_SESSION_NAME
            )
            # Install claude-code under a user-writable prefix. The Daytona
            # sandbox runs as non-root, so plain `npm install -g` fails with
            # EACCES on the system node_modules. We pin a per-user prefix and
            # later invoke claude via its absolute path (no PATH plumbing
            # needed across separate exec contexts).
            #
            # Synchronous (top-level) exec so claude is guaranteed installed
            # before any `claude --print` queues in the session — otherwise
            # user messages look like 30s+ hangs.
            logger.info("start: installing claude-code in sandbox %s", sandbox.id)
            install_cmd = (
                f"mkdir -p {_NPM_PREFIX} && "
                f"npm config set prefix {_NPM_PREFIX} && "
                f"npm install -g @anthropic-ai/claude-code 2>&1"
            )
            exit_code, output = await self._daytona.exec_sync(
                sandbox, command=install_cmd
            )
            logger.info(
                "start: npm install exit=%s; tail=%s",
                exit_code,
                (output or "")[-300:],
            )
            if exit_code != 0:
                raise RuntimeError(
                    f"claude-code install failed (exit={exit_code}): {output[-500:]}"
                )

            # Collect all files first so we know the total + can parallelize.
            # Filter out artifacts that bloat upload time without helping the
            # assistant: large binary state files and per-session noise.
            files: list[tuple[str, bytes]] = []
            trial_ids: list[str] = []
            total_bytes = 0
            async for rel, content in self._file_store.iter_files(
                experiment_id
            ):
                if _should_skip(rel):
                    continue
                trial_id = PurePosixPath(rel).parts[0]
                if trial_id not in trial_ids:
                    trial_ids.append(trial_id)
                files.append((rel, content))
                total_bytes += len(content)
            logger.info(
                "start: uploading %d files (%.1f MB) across %d trials",
                len(files),
                total_bytes / 1e6,
                len(trial_ids),
            )

            # Parallelize uploads with a bounded worker pool — Daytona's
            # per-call latency dominates, so concurrency cuts wall time
            # roughly N×. Cap to avoid socket exhaustion and rate limits.
            sem = asyncio.Semaphore(8)
            uploaded = [0]

            async def upload_one(rel: str, content: bytes) -> None:
                dest = f"{_WORKSPACE_ROOT}/jobs/{experiment_id}/{rel}"
                async with sem:
                    await self._daytona.upload_file(
                        sandbox, dest_path=dest, content=content
                    )
                uploaded[0] += 1
                if uploaded[0] % 25 == 0 or uploaded[0] == len(files):
                    logger.info(
                        "start: uploaded %d/%d", uploaded[0], len(files)
                    )

            await asyncio.gather(
                *(upload_one(rel, content) for rel, content in files)
            )

            claude_md = render_claude_md(
                experiment_id=experiment_id, trial_ids=trial_ids
            )
            await self._daytona.upload_file(
                sandbox,
                dest_path=f"{_WORKSPACE_ROOT}/CLAUDE.md",
                content=claude_md.encode("utf-8"),
            )

            await self._inject_skills(sandbox)
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

        # Build the shell command directly so we can:
        #   1. shlex-quote the user's message (it lands as the prompt arg
        #      after `--` and may contain spaces / shell metacharacters)
        #   2. redirect stdin from /dev/null so claude doesn't sit waiting
        #      for piped input ("Warning: no stdin data received in 3s")
        #
        # Required flags:
        #   --print + --output-format=stream-json need --verbose (claude
        #   refuses to start otherwise).
        parts: list[str] = [
            _CLAUDE_BIN,
            "--print",
            "--output-format=stream-json",
            "--verbose",
        ]
        if state.claude_session_id:
            parts += ["--resume", shlex.quote(state.claude_session_id)]
        parts += ["--", shlex.quote(content)]
        # `cd` into the workspace so claude finds CLAUDE.md and the
        # uploaded jobs/<exp>/ tree (Daytona session opens in $HOME).
        cmd_str = (
            f"cd {shlex.quote(_WORKSPACE_ROOT)} && "
            + " ".join(parts)
            + " < /dev/null"
        )

        logger.info(
            "send: session=%s sandbox=%s resume=%s content_len=%d",
            session_id,
            sandbox.id,
            bool(state.claude_session_id),
            len(content),
        )

        cmd_id = await self._daytona.exec_async(
            sandbox,
            daytona_session_id=state.daytona_session_id,
            command=cmd_str,
        )
        logger.info("send: session=%s cmd_id=%s execed", session_id, cmd_id)

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
                logger.info("send: session=%s stream_logs returned cleanly", session_id)
            except BaseException:
                logger.exception(
                    "send: session=%s stream_logs raised", session_id
                )
            finally:
                await queue.put(None)

        closer_task = asyncio.create_task(closer())

        leftover = ""
        n_events = 0
        n_stderr = 0
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                kind, chunk = item
                if kind == "stderr":
                    n_stderr += 1
                    logger.info(
                        "send: session=%s stderr[%d bytes]: %s",
                        session_id,
                        len(chunk),
                        chunk.rstrip()[:300],
                    )
                    yield {
                        "type": "_stderr",
                        "text": chunk,
                    }
                    continue
                logger.debug(
                    "send: session=%s stdout chunk[%d bytes]",
                    session_id,
                    len(chunk),
                )
                leftover += chunk
                while "\n" in leftover:
                    line, leftover = leftover.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        logger.info(
                            "send: session=%s invalid_json line: %s",
                            session_id,
                            line[:200],
                        )
                        yield {"type": "_invalid_json", "raw": line}
                        continue
                    if (
                        event.get("type") == "system"
                        and event.get("subtype") == "init"
                        and "session_id" in event
                    ):
                        state.claude_session_id = event["session_id"]
                    state.last_activity = _now()
                    n_events += 1
                    logger.info(
                        "send: session=%s event#%d type=%s subtype=%s",
                        session_id,
                        n_events,
                        event.get("type"),
                        event.get("subtype"),
                    )
                    yield event
            if leftover.strip():
                try:
                    yield json.loads(leftover.strip())
                except json.JSONDecodeError:
                    yield {"type": "_invalid_json", "raw": leftover.strip()}
        finally:
            logger.info(
                "send: session=%s stream done events=%d stderr_chunks=%d",
                session_id,
                n_events,
                n_stderr,
            )
            await closer_task

    async def export_skills(self, *, session_id: str) -> bytes:
        """Tar the sandbox's .claude/skills/ directory and return the archive bytes."""
        state = self._sessions.get(session_id)
        if state is None or state.broken:
            raise SessionNotFound(session_id)
        sandbox = self._sandbox_handles[session_id]
        archive_path = "/tmp/cc-chat-skills.tar.gz"
        skills_dir = f"{_WORKSPACE_ROOT}/.claude/skills"
        # `|| true` so an empty/missing skills dir gives an empty archive
        # rather than a non-zero exit.
        await self._daytona.exec_sync(
            sandbox,
            command=(
                f"mkdir -p {skills_dir} && "
                f"tar -czf {archive_path} -C {skills_dir} . || true"
            ),
        )
        return await self._daytona.download_file(
            sandbox, src_path=archive_path
        )

    async def _inject_skills(self, sandbox: CreatedSandbox) -> None:
        if self._skills_dir is None or not self._skills_dir.is_dir():
            return
        for path in self._skills_dir.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(self._skills_dir).as_posix()
            dest = f"{_WORKSPACE_ROOT}/.claude/skills/{rel}"
            await self._daytona.upload_file(
                sandbox, dest_path=dest, content=path.read_bytes()
            )

    async def close(self, *, session_id: str) -> None:
        state = self._sessions.pop(session_id)
        if state is None:
            return
        sandbox = self._sandbox_handles.pop(session_id, None)
        if sandbox is not None:
            try:
                await self._daytona.delete_sandbox(sandbox)
            except Exception:
                logger.exception(
                    "close: session=%s delete_sandbox raised", session_id
                )

    async def close_all(self) -> None:
        """Close every live session. Called on app shutdown."""
        sessions = list(self._sessions.iter_all())
        if not sessions:
            return
        logger.info("close_all: tearing down %d session(s)", len(sessions))
        await asyncio.gather(
            *(self.close(session_id=s.session_id) for s in sessions),
            return_exceptions=True,
        )

    def start_sweeper(self) -> None:
        """Start the background idle-session sweeper. Idempotent."""
        if self._sweeper_task is not None and not self._sweeper_task.done():
            return
        self._sweeper_task = asyncio.create_task(
            self._sweep_loop(), name="cc_chat_sweeper"
        )

    async def stop_sweeper(self) -> None:
        """Cancel the sweeper. Safe to call when not running."""
        task = self._sweeper_task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._sweeper_task = None

    async def _sweep_loop(self) -> None:
        logger.info(
            "sweeper: started (interval=%ss, idle_timeout=%s)",
            self._sweep_interval,
            self._idle_timeout,
        )
        try:
            while True:
                await asyncio.sleep(self._sweep_interval)
                try:
                    await self._sweep_once()
                except Exception:
                    logger.exception("sweeper: pass raised, continuing")
        except asyncio.CancelledError:
            logger.info("sweeper: cancelled")
            raise

    async def _sweep_once(self) -> None:
        now = _now()
        idle = list(
            self._sessions.idle(now=now, max_idle=self._idle_timeout)
        )
        if not idle:
            return
        logger.info(
            "sweeper: closing %d idle session(s) (idle > %s)",
            len(idle),
            self._idle_timeout,
        )
        for state in idle:
            try:
                await self.close(session_id=state.session_id)
            except Exception:
                logger.exception(
                    "sweeper: close failed for session=%s",
                    state.session_id,
                )
