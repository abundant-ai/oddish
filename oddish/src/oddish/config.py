import json
import os
from typing import ClassVar

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from harbor.agents.utils import PROVIDER_KEYS
from harbor.llms.utils import split_provider_model_name
from harbor.models.agent.name import AgentName
from litellm.litellm_core_utils.get_llm_provider_logic import get_llm_provider


_FIXED_AGENT_PROVIDERS: dict[str, str] = {
    AgentName.CLAUDE_CODE.value: "claude",
    AgentName.GEMINI_CLI.value: "gemini",
    AgentName.CODEX.value: "openai",
}

_MODEL_ABSENT_ALIASES: set[str] = {"", "-", "none", "null", "nil", "n/a", "na", "default"}
_PROVIDER_ONLY_QUEUE_ALIASES: set[str] = {
    "openai",
    "anthropic",
    "claude",
    "google",
    "gemini",
    "default",
}


def _build_agent_provider_map() -> dict[str, str]:
    """Maps Harbor agent names to API providers for rate limiting.

    Agents with a fixed provider affinity (CLI-based agents bound to a single
    LLM vendor) get explicit mappings.  All others default to "default" — the
    model-based detection in get_provider_for_trial() resolves the real
    provider at runtime.

    Built from Harbor's AgentName enum so new agents are picked up
    automatically.
    """
    return {
        name.value: _FIXED_AGENT_PROVIDERS.get(name.value, "default")
        for name in AgentName
    }


# Keep a compact provider map for usage/cost attribution and compatibility.
_MODEL_PROVIDER_ALIASES: dict[str, str] = {
    # Claude (direct + Bedrock)
    "anthropic": "claude",
    "claude": "claude",
    "bedrock": "claude",
    # Gemini / Google
    "gemini": "gemini",
    "google": "gemini",
    "vertex_ai": "gemini",
    "palm": "gemini",
}


def _normalize_model_provider(provider: str) -> str | None:
    normalized = provider.strip().lower()
    if not normalized:
        return None
    if normalized in _MODEL_PROVIDER_ALIASES:
        return _MODEL_PROVIDER_ALIASES[normalized]
    if normalized in PROVIDER_KEYS:
        return "openai"
    return None


def _get_provider_from_model(model_name: str) -> str | None:
    provider_prefix, _ = split_provider_model_name(model_name)
    if provider_prefix:
        return _normalize_model_provider(provider_prefix)
    try:
        _, llm_provider, _, _ = get_llm_provider(model=model_name)
    except Exception:
        llm_provider = None
    if llm_provider:
        return _normalize_model_provider(str(llm_provider))
    return None


def _infer_provider_prefix(model_name: str) -> str | None:
    """Infer a canonical provider prefix for a model name, if possible."""
    provider_prefix, _ = split_provider_model_name(model_name)
    if provider_prefix:
        normalized = provider_prefix.strip().lower()
        return normalized or None

    try:
        _, llm_provider, _, _ = get_llm_provider(model=model_name)
    except Exception:
        llm_provider = None
    if llm_provider:
        normalized = str(llm_provider).strip().lower()
        return normalized or None

    # Heuristic fallback for common bare model aliases.
    lowered = model_name.strip().lower()
    if lowered.startswith("gpt-") or lowered.startswith(
        ("o1", "o3", "o4", "chatgpt-", "text-embedding-")
    ):
        return "openai"
    if lowered.startswith("claude"):
        return "anthropic"
    if lowered.startswith("gemini"):
        return "google"

    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ODDISH_",
        extra="ignore",
    )

    # ==========================================================================
    # HARDCODED DEFAULTS - Edit this file to change
    # ==========================================================================

    # Worker behavior
    max_retries: ClassVar[int] = 5
    retry_backoff_base: ClassVar[int] = 60  # seconds
    retry_backoff_max: ClassVar[int] = 3600  # seconds
    worker_poll_interval: ClassVar[float] = 10.0  # seconds
    worker_batch_size: ClassVar[int] = 1
    trial_retry_timer_minutes: ClassVar[int] = 60
    auto_start_workers: ClassVar[bool] = True

    # Storage paths
    harbor_jobs_dir: ClassVar[str] = "/tmp/harbor-jobs"
    local_storage_dir: ClassVar[str] = "/tmp/oddish-tasks"

    # Default execution environment (daytona or docker)
    # Can be overridden via CLI: oddish run --env daytona
    harbor_environment: ClassVar[str] = "daytona"

    # API server
    api_host: ClassVar[str] = "0.0.0.0"
    api_port: ClassVar[int] = 8000

    # Database connection pools
    db_pool_min_size: ClassVar[int] = 2
    db_pool_max_size: ClassVar[int] = 20
    db_pool_max_overflow: ClassVar[int] = 10
    db_pool_size: ClassVar[int] = 5

    # Queue limits are now model-keyed. Use ODDISH_MODEL_CONCURRENCY_OVERRIDES
    # for per-model values and ODDISH_DEFAULT_MODEL_CONCURRENCY for fallback.
    default_model_concurrency: int = 8
    model_concurrency_overrides: dict[str, int] = Field(default_factory=dict)
    analysis_model: str = "claude-haiku-4-5"
    verdict_model: str = "gpt-5.2"

    # Agent to provider mapping
    agent_to_provider: ClassVar[dict[str, str]] = _build_agent_provider_map()

    # ==========================================================================
    # ENV-VAR CONFIGURABLE - Secrets and infrastructure only
    # ==========================================================================

    # Database (supports DATABASE_URL or ODDISH_DATABASE_URL)
    database_url: str = "postgresql+asyncpg://oddish:oddish@localhost:5432/oddish"

    # Asyncpg pool (pgqueuer) sizing
    # Defaults are intentionally small to avoid exhausting DB connections when
    # many worker processes are spawned.
    asyncpg_pool_min_size: int = 1
    asyncpg_pool_max_size: int = 4

    @model_validator(mode="before")
    @classmethod
    def check_database_url(cls, data: dict | None) -> dict:
        """Prefer DATABASE_URL, fallback to ODDISH_DATABASE_URL."""
        data = data or {}
        if db_url := os.getenv("DATABASE_URL") or os.getenv("ODDISH_DATABASE_URL"):
            data["database_url"] = db_url
        return data

    @property
    def asyncpg_url(self) -> str:
        """Database URL without +asyncpg prefix."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://")

    # S3 Storage (secrets)
    s3_enabled: bool = False
    s3_endpoint_url: str | None = None
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "data"
    s3_region: str = "us-east-1"

    # Task upload limits (MB)
    max_task_upload_mb: int = 50

    # API keys (read from env without ODDISH_ prefix)
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")

    # ==========================================================================
    # Helper methods
    # ==========================================================================

    @model_validator(mode="after")
    def normalize_model_overrides(self) -> "Settings":
        raw = os.getenv("ODDISH_MODEL_CONCURRENCY_OVERRIDES")
        if not raw:
            return self
        try:
            parsed = json.loads(raw)
        except Exception as exc:
            raise ValueError(
                "ODDISH_MODEL_CONCURRENCY_OVERRIDES must be valid JSON"
            ) from exc
        if not isinstance(parsed, dict):
            raise ValueError("ODDISH_MODEL_CONCURRENCY_OVERRIDES must be a JSON object")
        normalized: dict[str, int] = {}
        for key, value in parsed.items():
            queue_key = self.normalize_queue_key(str(key))
            normalized[queue_key] = int(value)
        self.model_concurrency_overrides = normalized
        return self

    def get_provider_for_agent(self, agent: str) -> str:
        """Return provider for agent (with prefix matching fallback)."""
        if agent in self.agent_to_provider:
            return self.agent_to_provider[agent]
        for agent_pattern, provider in self.agent_to_provider.items():
            if agent.startswith(agent_pattern):
                return provider
        return "default"

    def get_provider_for_trial(self, agent: str, model: str | None) -> str:
        """Return provider for a trial using model first, agent fallback."""
        normalized_model = self.normalize_trial_model(agent, model)
        if normalized_model:
            provider = _get_provider_from_model(normalized_model)
            if provider:
                return provider
        return self.get_provider_for_agent(agent)

    def normalize_trial_model(self, agent: str, model: str | None) -> str | None:
        """Canonicalize trial model input for storage/routing.

        - Treat '-', 'none', 'null', empty, etc as missing.
        - For nop/oracle, always force model to 'default'.
        - Otherwise return cleaned model (or None if missing).
        """
        cleaned: str | None = None
        if model is not None:
            candidate = model.strip()
            if candidate.lower() not in _MODEL_ABSENT_ALIASES:
                cleaned = candidate

        normalized_agent = (agent or "").strip().lower()
        if normalized_agent in {AgentName.NOP.value, AgentName.ORACLE.value}:
            return "default"

        return cleaned

    def normalize_queue_key(self, model: str) -> str:
        """Normalize queue keys for PGQueuer entrypoints.

        For model-like inputs without an explicit provider prefix, this attempts
        to infer the provider and returns `provider/model` so bare and prefixed
        variants collapse to one queue key.
        """
        normalized = model.strip().lower().replace(" ", "_")
        if not normalized or normalized in _MODEL_ABSENT_ALIASES:
            return "default"
        if normalized in _PROVIDER_ONLY_QUEUE_ALIASES:
            return "default"
        if "/" in normalized:
            provider_prefix, canonical = normalized.split("/", 1)
            if (
                provider_prefix in _PROVIDER_ONLY_QUEUE_ALIASES
                and canonical in _PROVIDER_ONLY_QUEUE_ALIASES
            ):
                return "default"
            return normalized

        provider_prefix = _infer_provider_prefix(normalized)
        if not provider_prefix:
            return normalized
        return f"{provider_prefix}/{normalized}"

    def get_queue_key_for_trial(self, agent: str, model: str | None) -> str:
        """Resolve queue key from model first, fallback to provider bucket."""
        normalized_model = self.normalize_trial_model(agent, model)
        if normalized_model:
            return self.normalize_queue_key(normalized_model)
        return "default"

    def get_analysis_queue_key(self) -> str:
        return self.normalize_queue_key(self.analysis_model)

    def get_verdict_queue_key(self) -> str:
        return self.normalize_queue_key(self.verdict_model)

    def get_model_concurrency(self, queue_key: str) -> int:
        normalized = self.normalize_queue_key(queue_key)
        override = self.model_concurrency_overrides.get(normalized)
        if override is not None:
            return max(int(override), 0)
        return max(int(self.default_model_concurrency), 0)

    def get_known_queue_keys(self) -> set[str]:
        keys = {self.get_analysis_queue_key(), self.get_verdict_queue_key()}
        keys.update(self.model_concurrency_overrides.keys())
        return keys

settings = Settings()
