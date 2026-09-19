"""Google Vertex AI provider plumbing: one environment profile for every trial.

Oddish makes ``vertex_ai/`` a real provider -- configuration with defaults, a
credential file, egress hosts, accounting -- and publishes the standard Vertex
environment that the ecosystem's harnesses already read: the Google Gen AI SDK
and Gemini CLI, LiteLLM, the Vercel AI SDK / OpenCode, and Claude Code.
Whether a given harness honors that environment is the harness's business;
nothing in this module is shaped per agent.

Two credential modes, resolved by ``Settings.vertex_ai_config``:

* service-account mode: the key JSON is written to a private worker file and
  uploaded into the sandbox by an ``AGENT_START`` hook (Modal and Daytona never
  fire Harbor's provisioned callback), then referenced by path;
* express mode: an API key only (Gemini models, global endpoint), published as
  a ``${VERTEX_AI_API_KEY}`` template that Harbor resolves from the worker
  process env, where the runner makes the key ambient.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from oddish.config import (
    VERTEX_AI_DEFAULT_LOCATION,
    VERTEX_AI_MODE_API_KEY,
    VERTEX_AI_MODE_SERVICE_ACCOUNT,
    VertexAiConfig,
    VertexAiConfigError,
    is_vertex_ai_claude_model,
    is_vertex_ai_model,
    settings,
    vertex_ai_bare_model_id,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from harbor.trial.hooks import TrialHookEvent

# Harbor's HookCallback, spelled out so this module (imported by the host
# discovery the CLI-safe modules reach) never loads harbor's trial package.
HookCallback = Callable[["TrialHookEvent"], Awaitable[None]]

# Where the upload hook places the service-account key inside the sandbox.
SANDBOX_CREDENTIALS_DIR = "/tmp/oddish-vertex"
SANDBOX_CREDENTIALS_PATH = f"{SANDBOX_CREDENTIALS_DIR}/service-account.json"
# Oddish-owned marker. Restricted profiles and host discovery key on it, so
# they never infer the mode from which credential names happen to be present
# (a caller-submitted ``GOOGLE_GENAI_USE_VERTEXAI`` without this marker is not
# an Oddish-routed Vertex trial and keeps failing closed).
MODE_ENV = "ODDISH_VERTEX_AI_MODE"
# A service-account key exchanges a signed JWT for an access token here (the
# key file's ``token_uri``). Express mode never needs it.
VERTEX_AUTH_HOSTS: tuple[str, ...] = ("oauth2.googleapis.com",)
# The GKE runtime materializes its own service account into the worker process
# from this variable (backend/worker/runtime.py) and the cluster client
# refreshes its tokens through ADC during a trial, so on such a worker the
# process-level ADC path must stay the GKE account.
_GKE_ADC_JSON_ENV = "GOOGLE_APPLICATION_CREDENTIALS_JSON"

# Claude Code's model variables. ``ANTHROPIC_MODEL`` is assigned outright
# (Harbor's own strip to the last path segment is conditional, it keeps the
# full id on the custom-base-URL branch); the aliases and the subagent model
# are defaulted to the trial's own model so background and subagent calls hit
# the one model the project enabled, instead of Claude Code's own Vertex
# default for small/fast work (``claude-sonnet-4-5@20250929``).
_CLAUDE_MODEL_KEY = "ANTHROPIC_MODEL"
_CLAUDE_MODEL_ALIAS_KEYS = (
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)

# Competing credentials and route selectors a Vertex trial must not inherit
# from the worker image or a submitted env: the image bakes Bedrock on, the
# Gen AI SDK and Gemini CLI prefer an API key over ADC, Gemini CLI's OAuth
# selection outranks its env auth type, and an ``ANTHROPIC_BASE_URL`` would put
# Harbor's Claude Code on its custom-endpoint branch. ``GEMINI_FORCE_OAUTH`` is
# a real boolean, not an empty string: Harbor parses it with
# ``parse_bool_env_value``, which raises on ``""``. The Anthropic API key and
# OAuth token are deliberately left alone: Claude Code's provider flag outranks
# them and LiteLLM ignores them on a ``vertex_ai/`` id.
_BLANKED_ENV: dict[str, str] = {
    "CLAUDE_CODE_USE_BEDROCK": "",
    "AWS_BEARER_TOKEN_BEDROCK": "",
    # Claude Code's other cloud-provider selector; a submitted value would
    # compete with CLAUDE_CODE_USE_VERTEX for the same precedence slot.
    "CLAUDE_CODE_USE_FOUNDRY": "",
    "GEMINI_API_KEY": "",
    "GOOGLE_GENERATIVE_AI_API_KEY": "",
    "GEMINI_FORCE_OAUTH": "false",
    "GEMINI_OAUTH_CREDS_PATH": "",
    "ANTHROPIC_BASE_URL": "",
}


def vertex_hosts(
    location: str | None, *, mode: str = VERTEX_AI_MODE_SERVICE_ACCOUNT
) -> list[str]:
    """Vertex endpoint host for *location*, plus the token host in SA mode.

    ``global`` (the default) and express mode use ``aiplatform.googleapis.com``;
    the ``us`` / ``eu`` multi-region locations use their ``rep`` hosts; any
    other value is a region such as ``us-east5``.
    """
    loc = (location or "").strip().lower() or VERTEX_AI_DEFAULT_LOCATION
    if mode == VERTEX_AI_MODE_API_KEY or loc == "global":
        endpoint = "aiplatform.googleapis.com"
    elif loc in ("us", "eu"):
        endpoint = f"aiplatform.{loc}.rep.googleapis.com"
    else:
        endpoint = f"{loc}-aiplatform.googleapis.com"
    hosts = [endpoint]
    if mode == VERTEX_AI_MODE_SERVICE_ACCOUNT:
        hosts.extend(VERTEX_AUTH_HOSTS)
    return hosts


def hosts_from_env(env: Mapping[str, str] | None) -> list[str] | None:
    """Hosts for an Oddish-marked Vertex profile in *env*, or ``None``."""
    if not env:
        return None
    mode = (env.get(MODE_ENV) or "").strip()
    if mode not in (VERTEX_AI_MODE_SERVICE_ACCOUNT, VERTEX_AI_MODE_API_KEY):
        return None
    return vertex_hosts(env.get("GOOGLE_CLOUD_LOCATION"), mode=mode)


def default_hosts() -> list[str]:
    """Hosts for a Vertex model when no profile env is at hand (read paths).

    Never raises: an unconfigured worker still resolves the endpoint for the
    configured location in service-account mode, so read-side callers such as
    the restricted-profile inference keep working.
    """
    try:
        config = settings.vertex_ai_config()
    except VertexAiConfigError:
        return vertex_hosts(settings.vertex_ai_location)
    return vertex_hosts(config.location, mode=config.mode)


def _coordinate_env(config: VertexAiConfig) -> dict[str, str]:
    env: dict[str, str] = {
        # Google Gen AI SDK / Gemini CLI, new and legacy names.
        "GOOGLE_GENAI_USE_VERTEXAI": "true",
        "GOOGLE_GENAI_USE_ENTERPRISE": "true",
        "GOOGLE_CLOUD_LOCATION": config.location,
        # LiteLLM.
        "VERTEXAI_LOCATION": config.location,
        # Vercel AI SDK / OpenCode.
        "GOOGLE_VERTEX_LOCATION": config.location,
        "VERTEX_LOCATION": config.location,
        # Claude Code.
        "CLAUDE_CODE_USE_VERTEX": "1",
        "CLOUD_ML_REGION": config.location,
        MODE_ENV: config.mode,
    }
    if config.project_id:
        env.update(
            {
                "GOOGLE_CLOUD_PROJECT": config.project_id,
                "VERTEXAI_PROJECT": config.project_id,
                "GOOGLE_VERTEX_PROJECT": config.project_id,
                "ANTHROPIC_VERTEX_PROJECT_ID": config.project_id,
            }
        )
    return env


def vertex_ai_agent_env(config: VertexAiConfig, model: str | None) -> dict[str, str]:
    """The profile published into ``agent_config.env`` (the sandbox overlay)."""
    env = dict(_BLANKED_ENV)
    env.update(_coordinate_env(config))
    if config.mode == VERTEX_AI_MODE_SERVICE_ACCOUNT:
        env["GOOGLE_APPLICATION_CREDENTIALS"] = SANDBOX_CREDENTIALS_PATH
        env["VERTEXAI_CREDENTIALS"] = SANDBOX_CREDENTIALS_PATH
        env["GOOGLE_API_KEY"] = ""
    else:
        # A template, never a literal: Harbor resolves it from the worker's
        # os.environ, where the runner makes the key ambient for the whole
        # config build and the Job scope. A literal would be redacted by
        # Harbor's env serializer (the name matches KEY).
        env["GOOGLE_API_KEY"] = "${VERTEX_AI_API_KEY}"
        env["VERTEXAI_CREDENTIALS"] = ""
    if is_vertex_ai_claude_model(model):
        bare = vertex_ai_bare_model_id(model or "")
        env[_CLAUDE_MODEL_KEY] = bare
        for key in _CLAUDE_MODEL_ALIAS_KEYS:
            env[key] = bare
    return env


def merge_agent_env(
    existing: Mapping[str, str] | None, config: VertexAiConfig, model: str | None
) -> dict[str, str]:
    """Overlay the profile onto a submitted env.

    Routing, credential, and blanked keys are assigned outright (a submitted
    value must not move a ``vertex_ai/`` trial off the platform account); the
    Claude alias pins are defaults a caller may still override.
    """
    merged = dict(existing or {})
    for key, value in vertex_ai_agent_env(config, model).items():
        if key in _CLAUDE_MODEL_ALIAS_KEYS:
            merged.setdefault(key, value)
        else:
            merged[key] = value
    return merged


def vertex_ai_process_env(
    config: VertexAiConfig, worker_path: Path | None
) -> dict[str, str]:
    """The worker-process scope: host-side harnesses and template resolution.

    Host-side LiteLLM harnesses read ``VERTEXAI_CREDENTIALS`` (LiteLLM prefers
    it over ADC and accepts a file path), so the credential that pays is the
    Vertex account even on a GKE worker, where ``GOOGLE_APPLICATION_CREDENTIALS``
    must stay the GKE account for the cluster client.
    """
    env = dict(_BLANKED_ENV)
    env.update(_coordinate_env(config))
    if config.mode == VERTEX_AI_MODE_SERVICE_ACCOUNT:
        if worker_path is None:
            raise VertexAiConfigError(
                "service-account mode needs the materialized worker credential file"
            )
        env["VERTEXAI_CREDENTIALS"] = str(worker_path)
        env["GOOGLE_API_KEY"] = ""
        if not os.environ.get(_GKE_ADC_JSON_ENV):
            env["GOOGLE_APPLICATION_CREDENTIALS"] = str(worker_path)
    else:
        env["GOOGLE_API_KEY"] = config.api_key or ""
        env["VERTEX_AI_API_KEY"] = config.api_key or ""
        env["VERTEXAI_CREDENTIALS"] = ""
    return env


def redaction_env(config: VertexAiConfig) -> dict[str, str]:
    """Secret VALUES for the runtime redaction map, under credential-shaped names.

    The provider fold in the runner reads only ``os.environ``; this covers a
    credential that exists only in Settings, and the key material inside the
    JSON as its own replacement.
    """
    env: dict[str, str] = {}
    if config.credentials_json:
        env["VERTEX_AI_CREDENTIALS_JSON"] = config.credentials_json
        try:
            private_key = json.loads(config.credentials_json).get("private_key")
        except (ValueError, AttributeError):
            private_key = None
        if isinstance(private_key, str) and private_key:
            env["VERTEX_AI_PRIVATE_KEY"] = private_key
    if config.api_key:
        env["VERTEX_AI_API_KEY"] = config.api_key
    return env


_WORKER_CREDENTIAL_FILES: dict[str, Path] = {}


def worker_credentials_path(config: VertexAiConfig) -> Path | None:
    """Write the service-account JSON once per process to a 0600 file.

    Google client libraries and Claude Code discover a service-account key only
    through a file path, never inline JSON (the same reason the GKE runtime
    materializes its key). Express mode has no file and returns ``None``.
    """
    if config.mode != VERTEX_AI_MODE_SERVICE_ACCOUNT or not config.credentials_json:
        return None
    digest = hashlib.sha256(config.credentials_json.encode("utf-8")).hexdigest()[:16]
    cached = _WORKER_CREDENTIAL_FILES.get(digest)
    if cached is not None and cached.is_file():
        return cached
    directory = Path(tempfile.gettempdir()) / "oddish-vertex"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"service-account-{digest}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(config.credentials_json)
    os.chmod(path, 0o600)
    _WORKER_CREDENTIAL_FILES[digest] = path
    return path


def agent_started_upload_hook(worker_path: Path) -> HookCallback:
    """Upload the worker's key file into the sandbox when the agent phase starts.

    ``AGENT_START`` is trial-driven, so it fires on every environment (Modal and
    Daytona never emit the provisioned callback) and carries the live
    environment handle. It fires before Harbor applies the agent user, and the
    effective user differs per step in multi-step trials, so the file is left
    root-owned and world-readable (0644) instead of chowned to a guess: every
    process that can read it already runs inside this trial's own sandbox with
    the trial's other plaintext provider keys in its env. Idempotent, so
    multi-step tasks re-run it safely; in-place retries reuse the same sandbox.
    """
    source = str(worker_path)

    async def upload(event: "TrialHookEvent") -> None:
        from harbor.trial.hooks import TrialEvent

        if event.event is not TrialEvent.AGENT_START:
            return
        environment = event.environment
        if environment is None:
            return
        await environment.exec(
            command=(
                f"mkdir -p {SANDBOX_CREDENTIALS_DIR} && chmod 755 {SANDBOX_CREDENTIALS_DIR}"
            ),
            user="root",
        )
        await environment.upload_file(source, SANDBOX_CREDENTIALS_PATH)
        await environment.exec(
            command=f"chmod 644 {SANDBOX_CREDENTIALS_PATH}", user="root"
        )

    return upload


@dataclass(frozen=True)
class VertexAiTrialPlan:
    """Everything the runner needs for one ``vertex_ai/`` trial."""

    config: VertexAiConfig
    worker_path: Path | None
    process_env: dict[str, str]
    redaction_env: dict[str, str]

    @property
    def upload_hook(self) -> HookCallback | None:
        if self.worker_path is None:
            return None
        return agent_started_upload_hook(self.worker_path)


def plan_for_model(
    model: str | None, *, extra_agent_env: Mapping[str, str] | None = None
) -> VertexAiTrialPlan | None:
    """Resolve the trial plan for a Vertex model, or ``None`` for any other id.

    A QA-gateway-routed analysis trial supplies its own Anthropic route and the
    routed config builder skips the profile for it; this returns ``None`` under
    the same condition so no credential is materialized or uploaded for it.
    Raises ``VertexAiConfigError`` for a Vertex model on an unconfigured worker
    so the trial fails at config build with the explicit message.
    """
    if not is_vertex_ai_model(model):
        return None
    if (extra_agent_env or {}).get("ODDISH_QA_MODEL_ROUTED") == "1":
        return None
    config = settings.vertex_ai_config()
    worker_path = worker_credentials_path(config)
    return VertexAiTrialPlan(
        config=config,
        worker_path=worker_path,
        process_env=vertex_ai_process_env(config, worker_path),
        redaction_env=redaction_env(config),
    )
