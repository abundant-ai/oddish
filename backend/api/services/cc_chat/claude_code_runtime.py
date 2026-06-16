from __future__ import annotations

import asyncio
import json
import logging
import shlex
import time
from dataclasses import dataclass
from typing import AsyncIterator, ClassVar

from api.services.cc_chat.daytona_client import CreatedSandbox, DaytonaClient

log = logging.getLogger("oddish.cc_chat.claude_code")


@dataclass
class AgentRunResult:
    transcript_path: str
    exit_code: int
    duration_s: float
    error: str | None = None

_NPM_PREFIX = "/home/daytona/.npm-global"
_CLAUDE_BIN = f"{_NPM_PREFIX}/bin/claude"
_WORKSPACE = "/home/daytona/workspace"
_TRANSCRIPT_PATH = f"{_WORKSPACE}/agent/transcript.jsonl"

# Headless `--print` runs can't surface an interactive approval prompt, so any
# tool the agent invokes (Bash `./oddish-query`, Read, etc.) would block on a
# permission gate that nothing can answer. The sandbox is ephemeral and
# isolated, so bypass permissions to let the agent actually use its tools.
_PERMISSION_FLAGS = ["--permission-mode", "bypassPermissions"]


class ClaudeCodeRuntime:
    name: ClassVar[str] = "claude-code"
    supported_models: ClassVar[list[str]] = [
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
    ]

    async def install(self, client: DaytonaClient, sandbox: CreatedSandbox) -> None:
        """Install claude-code + harbor into the sandbox.

        Each is skipped when already present, so a pre-baked snapshot (see
        ``cc_chat_daytona_snapshot``) turns this into two cheap existence checks
        instead of ~a minute of npm/pip. When both are missing (default image)
        they install concurrently rather than sequentially.
        """
        need_claude = not await self._present(
            client, sandbox, f"test -x {_CLAUDE_BIN}"
        )
        need_harbor = not await self._present(
            client, sandbox, "python -c 'import harbor'"
        )

        jobs = []
        if need_claude:
            jobs.append(self._install_claude(client, sandbox))
        if need_harbor:
            jobs.append(self._install_harbor(client, sandbox))
        if jobs:
            await asyncio.gather(*jobs)

    @staticmethod
    async def _present(client: DaytonaClient, sandbox: CreatedSandbox, check: str) -> bool:
        exit_code, _ = await client.exec_sync(sandbox, command=check)
        return exit_code == 0

    @staticmethod
    async def _install_claude(client: DaytonaClient, sandbox: CreatedSandbox) -> None:
        cmd = (
            f"mkdir -p {_NPM_PREFIX} && "
            f"npm config set prefix {_NPM_PREFIX} && "
            f"npm install -g @anthropic-ai/claude-code 2>&1"
        )
        exit_code, output = await client.exec_sync(sandbox, command=cmd)
        if exit_code != 0:
            raise RuntimeError(
                f"claude-code install failed (exit={exit_code}): {output[-500:]}"
            )

    @staticmethod
    async def _install_harbor(client: DaytonaClient, sandbox: CreatedSandbox) -> None:
        # Make the harbor package available so the agent can `import harbor` /
        # read its source (anti-cheat scanners, task scaffolding, etc.) without
        # having to reverse-engineer verifier scripts.
        exit_code, output = await client.exec_sync(
            sandbox,
            command="pip install --user --quiet harbor==0.5.0 2>&1",
        )
        if exit_code != 0:
            raise RuntimeError(
                f"harbor install failed (exit={exit_code}): {output[-500:]}"
            )

    async def run_once(
        self,
        client: DaytonaClient,
        sandbox: CreatedSandbox,
        *,
        instruction: str,
        model: str,
        timeout_s: int,
        anthropic_api_key: str | None = None,
    ) -> AgentRunResult:
        # Build a one-shot non-interactive run: pipe stream-json stdout
        # into a transcript file and stdout simultaneously via `tee`.
        await client.exec_sync(
            sandbox,
            command=f"mkdir -p {shlex.quote(_TRANSCRIPT_PATH.rsplit('/', 1)[0])}",
        )
        # claude-code CLI wants bare model ids; strip LiteLLM-style provider prefix.
        model = model.removeprefix("anthropic/")
        parts = [
            _CLAUDE_BIN,
            "--print",
            "--output-format=stream-json",
            "--verbose",
            *_PERMISSION_FLAGS,
            "--model",
            shlex.quote(model),
            "--",
            shlex.quote(instruction),
        ]
        cmd_str = (
            f"cd {shlex.quote(_WORKSPACE)} && "
            + " ".join(parts)
            + f" < /dev/null | tee {shlex.quote(_TRANSCRIPT_PATH)}"
        )

        cmd_id = await client.exec_async(
            sandbox,
            daytona_session_id="probe",
            command=cmd_str,
        )

        captured_stdout: list[str] = []
        captured_stderr: list[str] = []
        start = time.monotonic()
        exit_code: int = 0
        error: str | None = None

        async def on_stdout(chunk: str) -> None:
            captured_stdout.append(chunk)

        async def on_stderr(chunk: str) -> None:
            captured_stderr.append(chunk)

        try:
            await asyncio.wait_for(
                client.stream_logs(
                    sandbox,
                    daytona_session_id="probe",
                    cmd_id=cmd_id,
                    on_stdout=on_stdout,
                    on_stderr=on_stderr,
                ),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            error = "timed out"
            exit_code = -1
        except Exception as e:
            error = f"stream_logs error: {e}"
            exit_code = -1

        duration = time.monotonic() - start

        # Belt-and-suspenders: write our captured stdout to the transcript
        # path inside the sandbox in case `tee` didn't flush. Tests rely on this.
        body = ("".join(captured_stdout) + "".join(captured_stderr)).encode("utf-8")
        await client.upload_file(
            sandbox, dest_path=_TRANSCRIPT_PATH, content=body
        )

        return AgentRunResult(
            transcript_path=_TRANSCRIPT_PATH,
            exit_code=exit_code,
            duration_s=duration,
            error=error,
        )

    async def stream_chat(
        self,
        client: DaytonaClient,
        sandbox: CreatedSandbox,
        *,
        content: str,
        claude_session_id: str | None,
        daytona_session_id: str = "cc",
    ) -> AsyncIterator[dict]:
        parts = [
            _CLAUDE_BIN,
            "--print",
            "--output-format=stream-json",
            "--verbose",
            *_PERMISSION_FLAGS,
        ]
        if claude_session_id:
            parts += ["--resume", shlex.quote(claude_session_id)]
        parts += ["--", shlex.quote(content)]

        cmd_str = (
            f"cd {shlex.quote(_WORKSPACE)} && "
            + " ".join(parts)
            + " < /dev/null"
        )

        cmd_id = await client.exec_async(
            sandbox,
            daytona_session_id=daytona_session_id,
            command=cmd_str,
        )

        queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()

        async def on_stdout(chunk: str) -> None:
            await queue.put(("stdout", chunk))

        async def on_stderr(chunk: str) -> None:
            await queue.put(("stderr", chunk))

        stream_task = asyncio.create_task(
            client.stream_logs(
                sandbox,
                daytona_session_id=daytona_session_id,
                cmd_id=cmd_id,
                on_stdout=on_stdout,
                on_stderr=on_stderr,
            )
        )

        async def closer() -> None:
            try:
                await stream_task
            except BaseException:
                pass
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
                    yield {"type": "_stderr", "text": chunk}
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
                    yield event
            if leftover.strip():
                try:
                    yield json.loads(leftover.strip())
                except json.JSONDecodeError:
                    yield {"type": "_invalid_json", "raw": leftover.strip()}
        finally:
            try:
                await closer_task
            except (asyncio.CancelledError, Exception):
                pass
