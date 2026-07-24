"""Claude Code as an analyzer backend, running on this process's filesystem.

The sandbox backend ships the files to a VM and drives claude-code there; this
one runs it as a subprocess over directories the worker already has on disk.
Both are ordinary ``AnalyzerLLMClient``s built from declarative config, so an
``AnalyzerBlock`` provisions either one the same way.

Output parsing is shared: ``--output-format json`` (here) and stream-json (the
sandbox) differ only in how the final envelope is framed, not in what it holds.
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

        return json.loads(Block.strip_code_fences(result))
    raise RuntimeError("claude-code envelope carried no structured result")


def parse_cli_envelope(raw: str) -> Any:
    """``output_transform`` for ``--output-format json``: one object."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Claude CLI returned invalid JSON: {exc}") from exc
    return extract_claude_result(payload)


def parse_stream_json_result(raw: str) -> Any:
    """``output_transform`` for stream-json: the last ``result`` event wins."""
    decoder = json.JSONDecoder()
    offset = 0
    events: list[dict] = []
    while offset < len(raw):
        event, offset = decoder.raw_decode(raw, offset)
        if isinstance(event, dict):
            events.append(event)
        while offset < len(raw) and raw[offset].isspace():
            offset += 1
    for event in reversed(events):
        if event.get("type") == "result":
            return extract_claude_result(event)
    raise RuntimeError("sandbox run emitted no structured result event")


class ClaudeCliClient:
    """claude-code in print mode, bound to the directories in ``CliConfig``.

    Yields the raw stdout envelope as a single chunk; the block's
    ``output_transform`` (``parse_cli_envelope``) turns it into the object.
    Usage is read off the same envelope, matching how the sandbox client reads
    it off the stream's ``result`` event.
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
            "--output-format",
            "json",
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
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=config.timeout
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise TimeoutError from None

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        if config.verbose:
            print_process_stream("Claude stderr", stderr_text, Colors.MAGENTA)

        if process.returncode != 0:
            error_text = (
                stderr_text.strip() or stdout_text.strip() or "Unknown Claude CLI error"
            )
            raise RuntimeError(
                f"Claude CLI exited with code {process.returncode}: {error_text}"
            )

        try:
            self.last_usage = parse_cli_usage(json.loads(stdout_text), model_id)
        except json.JSONDecodeError:
            # Leave the envelope to parse_cli_envelope, which owns the error
            # message the caller sees. Usage is simply unavailable.
            if config.verbose:
                print_process_stream("Claude stdout", stdout_text, Colors.BLUE)

        yield stdout_text

    async def aclose(self) -> None:
        """No-op: the subprocess is reaped before ``stream`` returns."""
        return None
