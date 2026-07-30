"""Claude Code as an analyzer backend, running on this process's filesystem.

The sandbox backend ships the files to a VM and drives claude-code there; this
one runs it as a subprocess over directories the worker already has on disk.
Both are ordinary ``AnalyzerLLMClient``s built from declarative config, so an
``AnalyzerBlock`` provisions either one the same way.

Both backends emit stream-json events and share ``parse_stream_json_result``,
so the analysis log can render live progress from either one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from oddish.analyze._sdk_utils import Colors, print_process_stream
from oddish.analyze.analysis_cost import AnalysisUsage, parse_cli_usage
from oddish.config import (
    BEDROCK_ENV_VARS,
    looks_like_bedrock_model_id,
    settings,
    to_anthropic_api_model_id,
)

logger = logging.getLogger(__name__)

# stream-json puts a whole event on one line, and a result event carries the
# full report -- far past asyncio's 64 KiB default readline limit, which
# raises instead of truncating.
_STREAM_LIMIT_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class CliConfig:
    """The filesystem a local claude-code run is allowed to see.

    ``cwd`` is required: claude-code resolves relative paths and its default
    read scope against it, so an unset cwd silently points the agent at whatever
    directory the worker happens to be running in.
    """

    cwd: Path
    add_dirs: tuple[Path, ...] = ()
    allowed_tools: tuple[str, ...] = ("Read", "Glob")
    json_schema: str | None = None
    timeout: float | None = None
    verbose: bool = False


def resolve_analysis_model_and_env(
    model: str, base_env: dict[str, str]
) -> tuple[str, dict[str, str]]:
    """Pick the ``--model`` id and env for the analysis Claude CLI subprocess.

    The Modal image bakes the Bedrock env vars so Claude Code defaults to
    Bedrock, and the CLI picks its route from the environment (not ``--model``).
    Two cases route this analysis call to the direct Anthropic API instead:

    * a plain (non-Bedrock) analysis model id, or
    * the force-direct incident toggle (``settings.claude_code_force_direct_api``,
      default on; mirrors ``harbor_runner._claude_code_forces_direct_api``) when
      an ``ANTHROPIC_API_KEY`` is present -- the workers' Bedrock credentials
      can't run inference (400 "Operation not allowed"), so every Bedrock
      analysis call fails until that flag is flipped off.

    In both direct-API cases, normalize any Bedrock inference-profile id back to
    its plain API id and strip the Bedrock signals so the CLI authenticates with
    ``ANTHROPIC_API_KEY``. Otherwise keep the Bedrock id and env untouched.
    """
    env = dict(base_env)
    has_api_key = bool(env.get("ANTHROPIC_API_KEY", "").strip())
    force_direct = has_api_key and settings.claude_code_force_direct_api
    if looks_like_bedrock_model_id(model) and not force_direct:
        return model, env
    for name in BEDROCK_ENV_VARS:
        env.pop(name, None)
    return (to_anthropic_api_model_id(model) or model), env


def extract_claude_result(payload: dict) -> Any:
    """Pull the structured object out of a claude-code result envelope."""
    # Not `.get(a, .get(b))`: claude-code emits an explicit
    # "structured_output": null when schema validation produced nothing, and
    # that must still fall through to the free-text result.
    result = payload.get("structured_output")
    if result is None:
        result = payload.get("result")
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        from oddish.blocks.block import Block

        # parse_json, not json.loads(strip_code_fences(...)): a free-text result
        # is the model talking, and it routinely writes a sentence before its
        # fenced object. parse_json owns the recovery for that, and this path
        # must not fork from it -- pre-trial audits reach validation only
        # through here.
        return Block.parse_json(result)
    raise RuntimeError("claude-code envelope carried no structured result")


def parse_stream_json_result(raw: str) -> Any:
    """``output_transform`` for stream-json: the last ``result`` event wins."""
    decoder = json.JSONDecoder()
    offset = 0
    events: list[dict] = []
    while offset < len(raw):
        try:
            event, offset = decoder.raw_decode(raw, offset)
        except json.JSONDecodeError:
            # Non-JSON noise between events (a stray CLI banner line, ANSI
            # codes) must not discard the result events around it.
            offset = raw.find("{", offset + 1)
            if offset == -1:
                break
            continue
        if isinstance(event, dict):
            events.append(event)
        while offset < len(raw) and raw[offset].isspace():
            offset += 1
    for event in reversed(events):
        if event.get("type") == "result":
            try:
                return extract_claude_result(event)
            except RuntimeError:
                # A stream may end with an empty result envelope after an
                # earlier result event already supplied the usable output.
                continue
    raise RuntimeError("analyzer run emitted no structured result event")


class ClaudeCliClient:
    """claude-code in print mode, bound to the directories in ``CliConfig``.

    Runs with ``--output-format stream-json`` and yields one event per line
    while the run is live, so callers (the analysis log) can stream progress.
    This is the same shape the sandbox client yields; both are parsed by
    ``parse_stream_json_result``. Usage is read off the stream's ``result``
    event.
    """

    def __init__(self, *, model: str | None = None, config: CliConfig) -> None:
        self._model = model or ""
        self._config = config
        self.last_usage: AnalysisUsage | None = None

    async def stream(
        self, prompt: str, *, system_prompt: str | None = None
    ) -> AsyncIterator[str]:
        self.last_usage = None
        config = self._config
        claude_bin = os.getenv("CC_LOGGER_REAL_CLAUDE") or "claude"
        model_id, env = resolve_analysis_model_and_env(self._model, dict(os.environ))
        logger.info("claude cli model=%s (from %s)", model_id, self._model)

        command = [
            claude_bin,
            "-p",
            prompt,
            "--model",
            model_id,
            # stream-json emits one event per line while the run is live.
            # Print mode requires --verbose with stream-json.
            "--output-format",
            "stream-json",
            "--verbose",
            "--tools",
            ",".join(config.allowed_tools),
            "--allowedTools",
            *config.allowed_tools,
            "--permission-mode",
            "bypassPermissions",
            "--dangerously-skip-permissions",
        ]
        if config.json_schema:
            command += ["--json-schema", config.json_schema]
        if system_prompt:
            command += ["--append-system-prompt", system_prompt]
        for add_dir in config.add_dirs:
            command += ["--add-dir", str(add_dir)]

        if config.verbose:
            print(
                f"{Colors.CYAN}[Classifier] Claude CLI model={model_id} "
                f"cwd={config.cwd}{Colors.RESET}",
                flush=True,
            )

        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(config.cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=_STREAM_LIMIT_BYTES,
        )
        # Drain stderr in the background so a full pipe cannot block the run.
        stderr_task = asyncio.create_task(process.stderr.read())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + config.timeout if config.timeout else None
        # On a nonzero exit with empty stderr, the last stdout event usually
        # carries the real failure text.
        last_line = ""
        try:
            try:
                while True:
                    if deadline is not None:
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            raise asyncio.TimeoutError
                        line = await asyncio.wait_for(
                            process.stdout.readline(), timeout=remaining
                        )
                    else:
                        line = await process.stdout.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue
                    try:
                        event = json.loads(text)
                    except json.JSONDecodeError:
                        # Keep the line for error context, but do not yield it:
                        # the chunks are joined and parsed as adjacent JSON.
                        last_line = text
                        continue
                    if isinstance(event, dict) and event.get("type") == "result":
                        usage = parse_cli_usage(event, model_id)
                        if usage is not None:
                            self.last_usage = usage
                    last_line = text
                    yield text
                await process.wait()
            except (asyncio.TimeoutError, TimeoutError):
                process.kill()
                await process.wait()
                raise TimeoutError from None

            stderr_text = await stderr_task
            stderr_decoded = stderr_text.decode("utf-8", errors="replace")
            if config.verbose:
                print_process_stream("Claude stderr", stderr_decoded, Colors.MAGENTA)

            if process.returncode != 0:
                error_text = (
                    stderr_decoded.strip() or last_line or "Unknown Claude CLI error"
                )
                raise RuntimeError(
                    f"Claude CLI exited with code {process.returncode}: {error_text}"
                )
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            if not stderr_task.done():
                stderr_task.cancel()

    async def aclose(self) -> None:
        """No-op: the subprocess is reaped before ``stream`` returns."""
        return None
