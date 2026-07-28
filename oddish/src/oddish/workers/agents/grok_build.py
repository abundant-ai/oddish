from __future__ import annotations

import json
import os
import random
import re
import shlex
import tempfile
from typing import Any

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from .grok_build_session import (
    GROK_SESSION_CAPTURE_DIRNAME,
    build_session_trajectory,
)
from .grok_build_trajectory import write_grok_build_trajectory_if_richer

from harbor.utils.trajectory_utils import format_trajectory_json


_OUTPUT_FILENAME = "grok-build.json"
_STDERR_FILENAME = "grok-build.stderr.log"
_DEFAULT_MODEL = "v9m-rl-learnability-tp8"
_XAI_BASE_URL = "https://api.x.ai/v1"
_XAI_API_KEY_ENV = "XAI_API_KEY"
# Optional comma-separated key pool; one key is drawn per trial to spread load
# across accounts. This only buys headroom if the keys belong to *different* xAI
# teams: the throttle is team-scoped ("You've hit your team's API rate limit"),
# so a pool of keys on one team shares a single bucket and concurrent trials
# throttle exactly as they do with one key.
_XAI_API_KEYS_ENV = "XAI_API_KEYS"

# Where the grok CLI persists its full session store (tool calls + token usage);
# the headless stdout does not carry these, so we copy this tree into the trial
# logs so it survives sandbox teardown and can be converted to a trajectory.
_SESSION_CAPTURE_PATH = f"/logs/agent/{GROK_SESSION_CAPTURE_DIRNAME}"

# Valid xAI transport backends the Grok CLI understands for a ``[model.*]``
# entry. Grok routes ``responses`` -> ``POST /v1/responses`` and
# ``chat_completions`` -> ``POST /v1/chat/completions``. The upstream Harbor
# ``GrokBuild`` hardcodes ``responses``, but not every xAI model is served on
# the Responses API: some (notably newer / unreleased models) are only exposed
# on Chat Completions and answer a Responses request with a 404
# ``The model <id> does not exist or your team does not have access to it``.
# Making the backend selectable lets a trial route such a model correctly
# without editing Harbor.
_VALID_API_BACKENDS = frozenset({"chat_completions", "responses", "messages"})
_API_BACKEND_RE = re.compile(r'api_backend = "[^"]*"')

# The grok CLI's stream watchdog ("inference idle timeout after 600s with no
# chunks") is fatal even though the CLI retries explicit transport errors up to
# 15x: one silently dropped stream destroys the whole run, however many turns
# it has completed. The stall is server-side (in-flight streams killed on the
# shared xAI deployment) and a fresh request lands on a healthy replica, so on
# that specific death we resume the session with ``grok -c`` (sessions are
# keyed by cwd, and the pre-run ``rm -rf`` guarantees the most recent session
# is this trial's) instead of throwing the run away. The failed call never
# committed to the session store, so a resume loses at most one turn.
_IDLE_TIMEOUT_ALTERNATIVES = "idle timeout"

# xAI rate limits ("You've hit your team's API rate limit") are the other death
# that destroys an otherwise healthy run: the CLI exits non-zero and the trial
# is thrown away mid-implementation, having already spent its turns. Unlike the
# idle timeout this is *not* fixed by an immediate retry -- the throttle is on
# the account, so a resume that fires straight away hits the same wall -- but
# xAI's limits are refilling token buckets, so a resume that waits often lands.
# Hence the shared resume loop below sleeps with an exponential backoff before
# replaying a rate-limited arm, and resumes idle timeouts immediately. When the
# limit is credit exhaustion rather than a bucket the backoff simply defers the
# same failure, which is no worse than failing now.
_RATE_LIMIT_ALTERNATIVES = "rate limit|rate_limit|too many requests|429"
_RATE_LIMIT_PATTERN = f"'({_RATE_LIMIT_ALTERNATIVES})'"

# xAI server-side 5xx / overload ("API error (status 503 Service Unavailable):
# unavailable: Service temporarily unavailable. The model did not respond to
# this request.") is the third death that throws away an otherwise healthy run:
# a single dropped stream on the shared deployment exits the CLI non-zero after
# however many turns it has completed. Like the idle timeout the cure is landing
# on a healthy replica, but during a sustained outage an instant replay just
# re-hits the same dead deployment -- so these join the backoff branch (wait,
# then resume) rather than resuming immediately. The failed call never committed
# to the session store, so a resume loses at most one turn.
_SERVER_ERROR_ALTERNATIVES = (
    "50[0-9]|service unavailable|service temporarily unavailable|"
    "temporarily unavailable|overloaded|bad gateway|gateway timeout"
)
# Errors that resume AFTER a backoff (the server/account needs a moment to
# recover); idle timeouts resume with no delay.
_BACKOFF_PATTERN = f"'({_RATE_LIMIT_ALTERNATIVES}|{_SERVER_ERROR_ALTERNATIVES})'"
_RESUMABLE_ERROR_PATTERN = (
    f"'({_IDLE_TIMEOUT_ALTERNATIVES}|{_RATE_LIMIT_ALTERNATIVES}"
    f"|{_SERVER_ERROR_ALTERNATIVES})'"
)
# First backoff, doubled per resume: 60s, 120s, 240s, 480s, 960s (~28m worst
# case, against a multi-hour agent timeout). Raised from 3 resumes so a sustained
# xAI 5xx window can outlast a few refill/recovery cycles rather than dying to it.
_RATE_LIMIT_BACKOFF_SEC = 60
_MAX_RESUMES = 5
_RESUME_PROMPT = (
    "The previous request failed with a transient API error. "
    "Continue the original task from where you left off."
)

# The rendered instruction is staged inside the sandbox as a file and read back
# via ``"$(cat ...)"`` instead of being inlined into the ``grok -p`` argv. Modal
# rejects any ``exec`` whose CMD arguments exceed 65536 bytes (ARG_MAX), and a
# large task instruction -- embedded up to three times across the CLI fallbacks
# -- blows past that limit and fails the whole trial during image/agent start.
# Uploading the prompt out-of-band keeps the exec command string tiny; the
# ``$(cat ...)`` substitution is expanded by the sandbox shell (bound only by
# the far larger in-sandbox Linux ARG_MAX), so grok still receives the full
# instruction as its ``-p`` argument.
_PROMPT_PATH = "/tmp/oddish-grok-build-prompt.txt"


def _positive_int(name: str, value: int | str | None) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        parsed = int(str(value).strip())
    except ValueError:
        parsed = 0
    if parsed <= 0:
        raise ValueError(
            f"grok-build {name} must be a positive integer, got {value!r}."
        )
    return parsed


class OddishGrokBuild(BaseInstalledAgent):
    """Grok Build wrapper that preserves streaming events for ATIF conversion."""

    SUPPORTS_ATIF: bool = True

    def __init__(
        self,
        *args: Any,
        reasoning_effort: str | None = "high",
        api_backend: str | None = None,
        max_retries: int | str | None = None,
        inference_idle_timeout_secs: int | str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._api_key: str | None = None
        self.reasoning_effort = reasoning_effort
        normalized_backend = (api_backend or "").strip()
        if normalized_backend and normalized_backend not in _VALID_API_BACKENDS:
            raise ValueError(
                f"Unsupported grok-build api_backend {api_backend!r}; "
                f"expected one of {sorted(_VALID_API_BACKENDS)}."
            )
        self.api_backend = normalized_backend or None
        # Grok's documented-but-unexplained ``[model.*]`` reliability knobs
        # (the settings reference describes both as just "Reliability."). Left
        # out of the config unless set via ``--agent-kwarg``; whether
        # ``max_retries`` covers the idle-timeout death is unverified upstream,
        # so these exist to canary that question, not as the fix.
        self.max_retries = _positive_int("max_retries", max_retries)
        self.inference_idle_timeout_secs = _positive_int(
            "inference_idle_timeout_secs", inference_idle_timeout_secs
        )

    @staticmethod
    def name() -> str:
        return "grok-build"

    def get_version_command(self) -> str | None:
        return 'export PATH="$HOME/.local/bin:$HOME/.grok/bin:$PATH"; grok --version'

    @classmethod
    def required_outbound_domains(
        cls, model_name: str | None = None, kwargs: dict[str, Any] | None = None
    ) -> list[str]:
        return ["api.x.ai"]

    async def install(self, environment: BaseEnvironment) -> None:
        await self.exec_as_root(
            environment,
            command=(
                "if command -v apt-get >/dev/null 2>&1; then "
                "DEBIAN_FRONTEND=noninteractive apt-get update && "
                "DEBIAN_FRONTEND=noninteractive apt-get install -y curl bash; "
                "elif command -v apk >/dev/null 2>&1; then "
                "apk add --no-cache curl bash; "
                "fi"
            ),
        )
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                "curl -fsSL https://x.ai/cli/install.sh | bash; "
                'export PATH="$HOME/.local/bin:$HOME/.grok/bin:$PATH"; '
                "command -v grok; "
                "grok --version"
            ),
        )

    def _resolve_model(self) -> str:
        if not self.model_name:
            return _DEFAULT_MODEL
        provider, separator, model = self.model_name.partition("/")
        if separator and provider.lower() == "xai":
            return model
        return self.model_name

    @staticmethod
    def _toml_string(value: str) -> str:
        return json.dumps(value)

    @staticmethod
    def _toml_table_key(value: str) -> str:
        if all(ch.isalnum() or ch in "-_" for ch in value):
            return value
        return OddishGrokBuild._toml_string(value)

    def build_config_toml(self) -> str:
        """Emit the grok config, honoring an ``api_backend`` override.

        Upstream Harbor pins every ``[model.*]`` block to
        ``api_backend = "responses"``. When ``api_backend`` is supplied (via an
        agent kwarg, e.g. ``--agent-kwarg api_backend=chat_completions``), swap
        the transport for every model entry so the trial can reach a model that
        is only served on that endpoint. When unset, the upstream default is
        preserved verbatim. ``max_retries`` / ``inference_idle_timeout_secs``
        are likewise appended to every model entry only when set.
        """
        model = self._resolve_model()
        quoted_model = self._toml_string(model)
        quoted_base_url = self._toml_string(_XAI_BASE_URL)
        quoted_env_key = self._toml_string(_XAI_API_KEY_ENV)
        reliability = []
        if self.max_retries is not None:
            reliability.append(f"max_retries = {self.max_retries}")
        if self.inference_idle_timeout_secs is not None:
            reliability.append(
                f"inference_idle_timeout_secs = {self.inference_idle_timeout_secs}"
            )
        config = "\n".join(
            [
                "disable_web_search = true",
                "[models]",
                f"default = {quoted_model}",
                f"web_search = {quoted_model}",
                f"session_summary = {quoted_model}",
                f"image_description = {quoted_model}",
                "[cli]",
                'installer = "internal"',
                f"[model.{self._toml_table_key(model)}]",
                f"name = {quoted_model}",
                f"model = {quoted_model}",
                f"base_url = {quoted_base_url}",
                f"env_key = {quoted_env_key}",
                'api_backend = "responses"',
                "context_window = 256000",
                *reliability,
                "[model.grok-build]",
                'name = "grok-build"',
                f"model = {quoted_model}",
                f"base_url = {quoted_base_url}",
                f"env_key = {quoted_env_key}",
                'api_backend = "responses"',
                "context_window = 256000",
                *reliability,
                "",
            ]
        )
        if not self.api_backend:
            return config
        return _API_BACKEND_RE.sub(
            f"api_backend = {self._toml_string(self.api_backend)}", config
        )

    async def _write_config(self, environment: BaseEnvironment) -> None:
        escaped_config = shlex.quote(self.build_config_toml())
        await self.exec_as_agent(
            environment,
            command=(
                f"mkdir -p ~/.grok && printf '%s\\n' {escaped_config} "
                "> ~/.grok/config.toml"
            ),
            env=self._xai_env(),
        )

    def _pick_api_key(self) -> str:
        pool = [
            key.strip()
            for key in (self._get_env(_XAI_API_KEYS_ENV) or "").split(",")
            if key.strip()
        ]
        if pool:
            return random.choice(pool)
        return self._get_env(_XAI_API_KEY_ENV) or ""

    def _xai_env(self) -> dict[str, str]:
        # Draw once and memoize: this is called twice per trial (the config
        # write and the run), and a fresh draw per call would hand the CLI a
        # different key than the one the trial was configured with.
        if self._api_key is None:
            self._api_key = self._pick_api_key()
        return {_XAI_API_KEY_ENV: self._api_key} if self._api_key else {}

    async def setup(self, environment: BaseEnvironment) -> None:
        await super().setup(environment)
        await self._write_config(environment)

    async def _stage_prompt(
        self, environment: BaseEnvironment, instruction: str
    ) -> None:
        """Upload the instruction into the sandbox as a readable file.

        ``upload_file`` transfers the bytes out-of-band (not via the exec argv),
        which is exactly what keeps us under Modal's ARG_MAX. It copies in as
        root, so we chmod the file world-readable afterwards to guarantee the
        (possibly non-root) agent user can read it back.
        """
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        )
        try:
            tmp.write(instruction)
            tmp.flush()
            tmp.close()
            await environment.upload_file(tmp.name, _PROMPT_PATH)
        finally:
            os.unlink(tmp.name)

        await self.exec_as_root(
            environment,
            command=f"chmod 0644 {shlex.quote(_PROMPT_PATH)}",
        )

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        await self._write_config(environment)
        await self._stage_prompt(environment, instruction)

        # Read the prompt back inside the sandbox rather than inlining it: the
        # command substitution is expanded by the sandbox shell, so the argv
        # sent to the Modal SDK stays small regardless of instruction size.
        prompt_arg = f'"$(cat {shlex.quote(_PROMPT_PATH)})"'
        stdout_path = f"/logs/agent/{_OUTPUT_FILENAME}"
        stderr_path = f"/logs/agent/{_STDERR_FILENAME}"

        def grok_command(
            output_format: str,
            *,
            no_auto_update: bool,
            include_reasoning_effort: bool = True,
            resume: bool = False,
        ) -> str:
            parts = ["grok"]
            if resume:
                parts.append("-c")
            parts += [
                "-p",
                shlex.quote(_RESUME_PROMPT) if resume else prompt_arg,
                "--always-approve",
                "--output-format",
                output_format,
            ]
            reasoning_effort = (self.reasoning_effort or "").strip()
            if include_reasoning_effort and reasoning_effort:
                parts.extend(["--reasoning-effort", shlex.quote(reasoning_effort)])
            if no_auto_update:
                parts.append("--no-auto-update")
            return " ".join(parts)

        # Flag-fallback variants in chain order; ``rv`` tracks which one ran so
        # an idle-timeout resume replays the variant the installed CLI actually
        # accepted, not the primary flag set.
        variants: list[dict[str, Any]] = [
            {"output_format": "streaming-json", "no_auto_update": True},
            {
                "output_format": "streaming-json",
                "no_auto_update": True,
                "include_reasoning_effort": False,
            },
            {"output_format": "json", "no_auto_update": True},
            {
                "output_format": "json",
                "no_auto_update": True,
                "include_reasoning_effort": False,
            },
            {"output_format": "json", "no_auto_update": False},
            {
                "output_format": "json",
                "no_auto_update": False,
                "include_reasoning_effort": False,
            },
        ]

        def arm(index: int) -> str:
            return grok_command(**variants[index])

        def resume_arm(index: int) -> str:
            return grok_command(**variants[index], resume=True)

        reasoning_unsupported_pattern = "'(reasoning-effort|reasoning_effort)'"
        unsupported_pattern = (
            "'(streaming-json|output-format|no-auto-update|unknown option|"
            "unrecognized option|unexpected argument|invalid value|unsupported)'"
        )
        # Clear any prior grok sessions before the run so the session store holds
        # exactly one session afterwards. Worker containers are reused across
        # trials; without this the store accumulates multiple sessions and the
        # trajectory capture cannot unambiguously pick this trial's session.
        command = (
            "mkdir -p /logs/agent; "
            'export PATH="$HOME/.local/bin:$HOME/.grok/bin:$PATH"; '
            'GROK_HOME="${GROK_HOME:-$HOME/.grok}"; '
            'rm -rf "$GROK_HOME/sessions" "$GROK_HOME/logs" 2>/dev/null; '
            "set +e; "
            f"{arm(0)} "
            f">{stdout_path} 2>{stderr_path}; "
            "rc=$?; rv=0; "
            f"if [ $rc -ne 0 ] && grep -Eqi {reasoning_unsupported_pattern} {stderr_path}; then "
            f"{arm(1)} "
            f">{stdout_path} 2>{stderr_path}; "
            "rc=$?; rv=1; "
            "fi; "
            f"if [ $rc -ne 0 ] && grep -Eqi {unsupported_pattern} {stderr_path}; then "
            f"{arm(2)} "
            f">{stdout_path} 2>{stderr_path}; "
            "rc=$?; rv=2; "
            "fi; "
            f"if [ $rc -ne 0 ] && grep -Eqi {reasoning_unsupported_pattern} {stderr_path}; then "
            f"{arm(3)} "
            f">{stdout_path} 2>{stderr_path}; "
            "rc=$?; rv=3; "
            "fi; "
            "if [ $rc -ne 0 ] && grep -Eqi '(no-auto-update|unknown option|"
            f"unrecognized option|unexpected argument)' {stderr_path}; then "
            f"{arm(4)} "
            f">{stdout_path} 2>{stderr_path}; "
            "rc=$?; rv=4; "
            "fi; "
            f"if [ $rc -ne 0 ] && grep -Eqi {reasoning_unsupported_pattern} {stderr_path}; then "
            f"{arm(5)} "
            f">{stdout_path} 2>{stderr_path}; "
            "rc=$?; rv=5; "
            "fi; "
            # Resume (rather than fail the trial) when the run died to the
            # stream watchdog, an xAI rate limit, or an xAI 5xx/overload.
            # Appending to stdout keeps the streamed event log whole; overwriting
            # stderr makes each grep reflect only the latest attempt, so any other
            # failure (flag error) still exits the loop. A rate-limited or
            # server-error arm waits out the backoff first -- resuming instantly
            # would just re-hit the same throttle or dead deployment and burn the
            # resume budget in seconds -- while an idle timeout, whose cure is
            # landing on a fresh replica, resumes with no delay.
            f"resumes=0; delay={_RATE_LIMIT_BACKOFF_SEC}; "
            f"while [ $rc -ne 0 ] && [ $resumes -lt {_MAX_RESUMES} ] "
            f"&& grep -Eqi {_RESUMABLE_ERROR_PATTERN} {stderr_path}; do "
            "resumes=$((resumes+1)); "
            f"if grep -Eqi {_BACKOFF_PATTERN} {stderr_path}; then "
            'sleep "$delay"; delay=$((delay*2)); '
            "fi; "
            'case "$rv" in '
            + " ".join(
                f"{index}) {resume_arm(index)} >>{stdout_path} 2>{stderr_path};;"
                for index in range(len(variants))
            )
            + " esac; "
            "rc=$?; "
            "done; "
            "exit $rc"
        )
        try:
            await self.exec_as_agent(environment, command=command, env=self._xai_env())
        finally:
            # Capture on the failure path too: a crashed run still leaves the
            # session store (unified.jsonl and the tool-call trajectory) behind,
            # and without it a dead trial's only evidence is the terse stderr
            # JSON.
            await self._capture_session(environment)

    async def _capture_session(self, environment: BaseEnvironment) -> None:
        """Copy the grok session store into the trial logs.

        The headless stdout only carries the assistant's text; the tool calls
        and per-turn token usage live under ``$GROK_HOME/sessions`` (and
        ``logs``). Copy that tree into ``/logs/agent/grok-session`` so it is
        uploaded with the trial and can be turned into a rich trajectory. Fully
        best-effort: a copy failure must never fail the trial.
        """
        capture = shlex.quote(_SESSION_CAPTURE_PATH)
        # Copy both sessions/ (tool-call trajectory) and logs/ (the sampling log,
        # which carries per-request token usage the session stream omits).
        command = (
            "set +e; "
            f"mkdir -p {capture}; "
            'GROK_HOME="${GROK_HOME:-$HOME/.grok}"; '
            f'cp -a "$GROK_HOME/sessions" {capture}/ 2>/dev/null; '
            f'cp -a "$GROK_HOME/logs" {capture}/ 2>/dev/null; '
            "exit 0"
        )
        try:
            await self.exec_as_agent(environment, command=command)
        except Exception:
            self.logger.warning("Failed to capture grok session store", exc_info=True)

    def _session_id_from_output(self) -> str | None:
        output_path = self.logs_dir / _OUTPUT_FILENAME
        if not output_path.is_file():
            return None
        try:
            text = output_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        session_id: str | None = None
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                candidate = event.get("sessionId") or event.get("session_id")
                if isinstance(candidate, str) and candidate:
                    session_id = candidate
        return session_id

    def populate_context_post_run(self, context: AgentContext) -> None:
        super().populate_context_post_run(context)
        trajectory = None
        # Prefer the on-disk session store (real tool calls + token usage) over
        # the text-only headless stdout.
        try:
            session_trajectory = build_session_trajectory(
                self.logs_dir / GROK_SESSION_CAPTURE_DIRNAME,
                session_id=self._session_id_from_output(),
                agent_version=getattr(self, "_version", None) or "unknown",
                model_name=self.model_name,
            )
        except Exception:
            self.logger.warning(
                "Failed to build Grok Build session trajectory", exc_info=True
            )
            session_trajectory = None

        if session_trajectory and session_trajectory.steps:
            try:
                (self.logs_dir / "trajectory.json").write_text(
                    format_trajectory_json(session_trajectory.to_json_dict()),
                    encoding="utf-8",
                )
                trajectory = session_trajectory
            except Exception:
                self.logger.warning(
                    "Failed to write Grok Build session trajectory", exc_info=True
                )

        if trajectory is None:
            try:
                trajectory = write_grok_build_trajectory_if_richer(
                    existing_trajectory_path=self.logs_dir / "trajectory.json",
                    output_path=self.logs_dir / _OUTPUT_FILENAME,
                    agent_version=getattr(self, "_version", None) or "unknown",
                    model_name=self.model_name,
                )
            except Exception:
                self.logger.exception("Failed to write Grok Build trajectory fallback")
                return

        if trajectory and trajectory.final_metrics:
            metrics = trajectory.final_metrics
            context.n_input_tokens = metrics.total_prompt_tokens or 0
            context.n_cache_tokens = metrics.total_cached_tokens or 0
            context.n_output_tokens = metrics.total_completion_tokens or 0
            context.cost_usd = metrics.total_cost_usd
