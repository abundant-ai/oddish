"""Oddish's Claude Code agent wrappers.

All Claude Code trials deliver the task over stdin so task text is absent from
the process command line -- upstream Harbor does this itself now, so the
wrappers no longer patch the command. Probe trials additionally install Harbor
into the sandbox so the agent can inspect the harness.

Stock Harbor installs only the claude-code CLI into the sandbox (see
``harbor.agents.installed.claude_code.ClaudeCode.install``). Probe trials also
want the *harbor* package importable inside the sandbox so the agent can
``import harbor`` while exploring the harness. We pin the install to the exact
harbor the orchestrator is running -- read from the installed package's PEP 610
``direct_url.json`` (harbor is a git fork, not a PyPI release) and rendered as
a requirement the sandbox can actually install: probe sandbox images ship no
``git`` binary, so GitHub sources become a commit tarball rather than a
``git+`` reference. In a blessed-variant container that is the variant's
harbor; for an explicit override the caller can pass ``(source, sha)``.

The wrappers are selected by ``_apply_claude_code_oddish_wrapper`` in
:mod:`oddish.workers.harbor.agent_config`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
from importlib.metadata import Distribution, PackageNotFoundError, version
from typing import Any, cast, override

from harbor.agents.installed.base import (
    AgentAuthenticationError,
    ApiInternalServerError,
    ApiOverloadedError,
    ApiRateLimitError,
    BaseEnvironment,
    ModelNotFoundError,
    NetworkConnectionError,
    NonZeroAgentExitCodeError,
    UnknownApiError,
)
from harbor.agents.installed.claude_code import ClaudeCode

from oddish.core.harbor_source import harbor_sandbox_requirement
from oddish.workers.harbor.failure_info import (
    PROVIDER_FAILURE_FILENAME,
    ProviderFailureEvidence,
    classify_provider_failure,
)

logger = logging.getLogger(__name__)

_INSTALL_TRANSIENT_EXIT_CODES = frozenset({18, 28, 35, 52, 55, 56, 92})
_INSTALL_MAX_ATTEMPTS = 3
_INSTALL_RETRY_BASE_DELAY_SEC = 2.0
_PROVIDER_MAX_RESUMES = 5
_PROVIDER_RETRY_BASE_DELAY_SEC = 60.0
_PROVIDER_RETRY_MAX_DELAY_SEC = 960.0
_RESUME_PROMPT = (
    "The previous request failed with a transient provider API error. "
    "Continue the original task from where you left off."
)
_API_ERROR_MESSAGE_RE = re.compile(r"\bAPI error\b", re.IGNORECASE)
_API_STATUS_MESSAGE_RE = re.compile(r"\b(?:API error:\s*)?([45]\d\d)\b", re.IGNORECASE)


def _coerce_http_status(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


def _coerce_retry_after_seconds(event: dict[str, Any]) -> float | None:
    milliseconds = event.get("retry_after_ms", event.get("retryAfterMs"))
    try:
        if milliseconds is not None:
            return max(0.0, float(milliseconds) / 1000.0)
        seconds = event.get("retry_after", event.get("retryAfter"))
        return max(0.0, float(seconds)) if seconds is not None else None
    except (TypeError, ValueError):
        return None


def _event_string(event: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _claude_result_failure(event: Any) -> ProviderFailureEvidence | None:
    if not isinstance(event, dict) or event.get("type") != "result":
        return None

    is_error = event.get("is_error", event.get("isError"))
    terminal_reason = event.get("terminal_reason", event.get("terminalReason"))
    status = _coerce_http_status(
        event.get(
            "api_error_status",
            event.get("apiErrorStatus", event.get("http_status")),
        )
    )
    message = event.get("result")
    if not isinstance(message, str):
        error = event.get("error")
        message = _event_string(error, "message") if isinstance(error, dict) else None

    if is_error is False:
        return None
    has_api_error_signal = (
        terminal_reason == "api_error"
        or status is not None
        or is_error is True
        and message is not None
        and _API_ERROR_MESSAGE_RE.search(message) is not None
    )
    if not has_api_error_signal:
        return None

    if status is None and message:
        match = _API_STATUS_MESSAGE_RE.search(message)
        if match:
            status = int(match.group(1))
    return ProviderFailureEvidence(
        provider="claude-code",
        terminal_reason=str(terminal_reason or "api_error"),
        http_status=status,
        request_id=_event_string(event, "request_id", "requestId"),
        resume_token=_event_string(event, "session_id", "sessionId"),
        retry_after_seconds=_coerce_retry_after_seconds(event),
        summary=message,
    )


def parse_claude_provider_failure(
    *streams: str | None,
) -> ProviderFailureEvidence | None:
    """Parse only the final Claude result event from one process invocation."""
    for stream in streams:
        last_result: dict[str, Any] | None = None
        for raw_line in (stream or "").splitlines():
            line = raw_line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                last_result = event
        if last_result is not None:
            return _claude_result_failure(last_result)
    return None


def _provider_exception_class(
    failure: ProviderFailureEvidence,
) -> type[NonZeroAgentExitCodeError]:
    if failure.http_status in {401, 403}:
        return cast(type[NonZeroAgentExitCodeError], AgentAuthenticationError)
    if failure.http_status == 404:
        return cast(type[NonZeroAgentExitCodeError], ModelNotFoundError)
    if failure.http_status == 429:
        return cast(type[NonZeroAgentExitCodeError], ApiRateLimitError)
    if failure.http_status == 529:
        return cast(type[NonZeroAgentExitCodeError], ApiOverloadedError)
    if failure.http_status is not None and 500 <= failure.http_status < 600:
        return cast(type[NonZeroAgentExitCodeError], ApiInternalServerError)
    return cast(type[NonZeroAgentExitCodeError], UnknownApiError)


def _installed_harbor_git_pin() -> tuple[str, str] | None:
    """``(source, commit)`` of the orchestrator's git-installed harbor, or None.

    uv/pip record the git source + resolved commit in the package's PEP 610
    ``direct_url.json``; this reflects whatever harbor was baked into the running
    container (default or a blessed variant) without any per-trial threading.
    """
    try:
        raw = Distribution.from_name("harbor").read_text("direct_url.json")
    except PackageNotFoundError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    url = data.get("url")
    vcs_info = data.get("vcs_info") or {}
    commit = vcs_info.get("commit_id")
    if url and commit and vcs_info.get("vcs") == "git":
        return url, commit
    return None


def _pinned_harbor_requirement(
    source: str | None = None, sha: str | None = None
) -> str | None:
    """The pip requirement that installs the run's harbor into the sandbox.

    Emits a direct reference the sandbox can install WITHOUT a git binary
    (GitHub sources render as the commit tarball; see
    ``harbor_sandbox_requirement``) so the sandbox gets the same fork commit
    as the run: an explicit ``(source, sha)`` wins; otherwise the
    orchestrator's own git-installed harbor (via ``direct_url``); falling
    back to ``harbor==<version>`` only if harbor is a plain release.
    """
    if source and sha:
        return harbor_sandbox_requirement(source, sha)
    pin = _installed_harbor_git_pin()
    if pin is not None:
        return harbor_sandbox_requirement(*pin)
    try:
        return f"harbor=={version('harbor')}"
    except PackageNotFoundError:
        logger.warning("probe: harbor not installed in orchestrator; skipping pin")
        return None


def _pinned_oddish_requirement() -> str | None:
    """The pip requirement that installs an ``oddish`` CLI matching this
    orchestrator, so the sandbox's ``oddish pull`` speaks the same API/schema
    the server expects.

    Unlike harbor (a git fork with no PyPI release), oddish is published to
    PyPI, so pinning the exact installed version is enough -- no git
    ``direct_url`` resolution needed.
    """
    try:
        return f"oddish=={version('oddish')}"
    except PackageNotFoundError:
        logger.warning("pre-trial: oddish not installed in orchestrator; skipping pin")
        return None


class OddishClaudeCode(ClaudeCode):
    """Claude Code with Oddish-owned transport and continuation policy.

    Harbor keeps the prompt off ``claude``'s argv itself: it hands the
    instruction to the agent's stdin through a transient environment
    variable and tees the stream-json output to
    ``/logs/agent/claude-code.txt``. The app wrapper retries transient CLI
    installation downloads before discarding a sandbox, classifies structured
    provider result events, and resumes provider-interrupted sessions in the
    same workspace. Its import path is also a routing key: agent config
    selects it (``_ODDISH_CLAUDE_CODE_IMPORT_PATH``), the restricted-network
    compatibility profiles are keyed on it, and
    :class:`OddishProbeClaudeCode` derives from it.
    """

    _resume: bool

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._last_provider_failure: ProviderFailureEvidence | None = None
        self._resume_session_id: str | None = None

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        """Retry only transport failures from Claude's idempotent installer."""
        for attempt in range(1, _INSTALL_MAX_ATTEMPTS + 1):
            try:
                await super().install(environment)
                return
            except NetworkConnectionError as exc:
                if (
                    exc.return_code not in _INSTALL_TRANSIENT_EXIT_CODES
                    or attempt == _INSTALL_MAX_ATTEMPTS
                ):
                    raise
                delay = _INSTALL_RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1))
                logger.warning(
                    "Claude Code installer transport failed with exit %s; "
                    "retrying in %.1fs (%s/%s)",
                    exc.return_code,
                    delay,
                    attempt,
                    _INSTALL_MAX_ATTEMPTS,
                )
                await asyncio.sleep(delay)

    @override
    def _classify_exec_error(
        self, command: str, result: Any
    ) -> NonZeroAgentExitCodeError:
        failure = parse_claude_provider_failure(result.stdout, result.stderr)
        if failure is None:
            return super()._classify_exec_error(command, result)

        self._last_provider_failure = failure
        exception_class = _provider_exception_class(failure)
        if failure.http_status is None:
            text_error = super()._classify_exec_error(command, result)
            if type(text_error) is not NonZeroAgentExitCodeError:
                exception_class = type(text_error)
        summary_fields = [f"terminal_reason={failure.terminal_reason}"]
        if failure.http_status is not None:
            summary_fields.append(f"http_status={failure.http_status}")
        if failure.request_id:
            summary_fields.append(f"request_id={failure.request_id}")
        if failure.resume_token:
            summary_fields.append(f"session_id={failure.resume_token}")
        detail = (
            f"Command failed (exit {result.return_code}): {command}\n"
            f"stdout: {self._truncate_output(result.stdout)}\n"
            f"stderr: {self._truncate_output(result.stderr)}\n"
            f"Claude provider API failure ({', '.join(summary_fields)})"
        )
        return exception_class(detail, return_code=result.return_code)

    async def _persist_final_provider_failure(
        self,
        environment: BaseEnvironment,
        failure: ProviderFailureEvidence,
    ) -> None:
        """Write only the final failed invocation's normalized provider facts."""
        payload = json.dumps(failure.as_dict(), sort_keys=True)
        try:
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            path = self.logs_dir / PROVIDER_FAILURE_FILENAME
            temporary = path.with_suffix(".tmp")
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(path)
        except OSError:
            logger.warning("Could not persist provider failure sidecar locally")

        # Non-mounted Harbor environments download /logs/agent after the agent
        # phase. Mirror the sidecar there so that download cannot discard the
        # locally written copy. Failure classification remains best-effort; the
        # original provider exception must always be the one Harbor records.
        try:
            await environment.exec(
                command=(
                    "mkdir -p /logs/agent && "
                    f"printf %s {shlex.quote(payload)} > "
                    f"/logs/agent/{PROVIDER_FAILURE_FILENAME}"
                )
            )
        except Exception:
            logger.warning("Could not mirror provider failure sidecar into sandbox")

    async def _clear_provider_failure(self, environment: BaseEnvironment) -> None:
        """Remove evidence left by an earlier logical run in this log directory."""
        (self.logs_dir / PROVIDER_FAILURE_FILENAME).unlink(missing_ok=True)
        try:
            await environment.exec(
                command=f"rm -f /logs/agent/{PROVIDER_FAILURE_FILENAME}"
            )
        except Exception:
            logger.debug("Could not clear provider failure sidecar in sandbox")

    @override
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: Any
    ) -> None:
        """Resume structured transient API failures before Harbor stops the sandbox."""
        original_resume = self._resume
        original_session_id = self._resume_session_id
        next_instruction = instruction
        canonical_session_id: str | None = None
        try:
            await self._clear_provider_failure(environment)
            for resume_count in range(_PROVIDER_MAX_RESUMES + 1):
                self._last_provider_failure = None
                try:
                    await super().run(next_instruction, environment, context)
                    return
                except NonZeroAgentExitCodeError as exc:
                    failure = self._last_provider_failure
                    decision = (
                        classify_provider_failure(
                            failure,
                            exception_type=type(exc).__name__,
                        )
                        if failure is not None
                        else None
                    )
                    if canonical_session_id is None and failure is not None:
                        canonical_session_id = failure.resume_token
                    if (
                        failure is None
                        or decision is None
                        or not decision.retryable
                        or not canonical_session_id
                        or resume_count == _PROVIDER_MAX_RESUMES
                    ):
                        if failure is not None:
                            await self._persist_final_provider_failure(
                                environment, failure
                            )
                        raise

                    delay = failure.retry_after_seconds
                    if delay is None:
                        delay = _PROVIDER_RETRY_BASE_DELAY_SEC * (2**resume_count)
                    delay = min(max(delay, 0.0), _PROVIDER_RETRY_MAX_DELAY_SEC)
                    logger.warning(
                        "Claude provider failure %s; resuming session %s in %.1fs "
                        "(%s/%s)",
                        failure.http_status or failure.terminal_reason,
                        canonical_session_id,
                        delay,
                        resume_count + 1,
                        _PROVIDER_MAX_RESUMES,
                    )
                    await asyncio.sleep(delay)
                    self._resume = True
                    self._resume_session_id = canonical_session_id
                    next_instruction = _RESUME_PROMPT
        finally:
            self._resume = original_resume
            self._resume_session_id = original_session_id

    @override
    def _build_claude_command(self, escaped_instruction: str, extra_flags: str) -> str:
        command = cast(
            str,
            super()._build_claude_command(escaped_instruction, extra_flags),
        )
        if self._resume_session_id:
            command = command.replace(
                "--continue ",
                f"--resume {shlex.quote(self._resume_session_id)} ",
                1,
            )
            command = command.replace(
                "| tee /logs/agent/claude-code.txt",
                "| tee -a /logs/agent/claude-code.txt",
                1,
            )
        return command

    @override
    def _parse_total_cost_from_stream_json(self) -> float | None:
        """Sum per-process result costs when one logical run used resume."""
        stream_path = self.logs_dir / "claude-code.txt"
        try:
            lines = stream_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        total = 0.0
        found_result = False
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "result":
                continue
            try:
                total += float(event["total_cost_usd"])
                found_result = True
            except (KeyError, TypeError, ValueError):
                continue
        return total if found_result else None


class OddishProbeClaudeCode(OddishClaudeCode):
    """Claude Code plus the Harbor package installed for probe trials."""

    async def install(self, environment: BaseEnvironment) -> None:
        # Keep the stock CLI + system-package install.
        await super().install(environment)

        requirement = _pinned_harbor_requirement()
        if requirement is None:
            return

        # Best-effort: install() runs during agent setup, so a hard failure here
        # would fail the WHOLE probe trial. The harbor package is a convenience
        # for the agent's exploration, not load-bearing for the run, so mirror
        # stage_harbor_source and swallow+log failures instead of aborting.
        command = f"pip install --user --quiet {shlex.quote(requirement)}"
        try:
            await self.exec_as_agent(environment, command=command)
        except Exception:
            logger.exception(
                "probe: failed to install %s into the sandbox; "
                "the agent will run without an importable harbor",
                requirement,
            )
