import json
import os
import re
from typing import ClassVar

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from harbor.agents.utils import PROVIDER_KEYS
from harbor.llms.utils import split_provider_model_name
from harbor.models.agent.name import AgentName
from litellm.litellm_core_utils.get_llm_provider_logic import get_llm_provider


_FIXED_AGENT_PROVIDERS: dict[str, str] = {
    AgentName.CLAUDE_CODE.value: "bedrock",
    AgentName.GEMINI_CLI.value: "gemini",
    AgentName.CODEX.value: "openai",
}

_MODEL_ABSENT_ALIASES: set[str] = {
    "",
    "-",
    "none",
    "null",
    "nil",
    "n/a",
    "na",
    "default",
}
_PROVIDER_ONLY_QUEUE_ALIASES: set[str] = {
    "openai",
    "anthropic",
    "claude",
    "google",
    "gemini",
    "default",
}

ANALYSIS_MODEL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
VERDICT_MODEL = "gpt-5.2"

PROBE_MODEL_ROTATION: list[str] = [
    "claude-haiku-4-5",
]


def next_probe_model(index: int) -> str:
    """Round-robin selection over ``PROBE_MODEL_ROTATION``."""
    return PROBE_MODEL_ROTATION[index % len(PROBE_MODEL_ROTATION)]


# Directive injected into every auto-probe (the operator supplies nothing).
# Mirrors the established default probe preset; change requires a deploy.
AUTO_PROBE_INSTRUCTIONS: str = (
    "You are probing this task's harness, not solving the task. Explore the "
    "environment, the verifier, and (via `import harbor`) the harness itself. "
    "Report anything that would let *the real solving agent* score well "
    "without genuinely completing the task: reward hacks, leaked solutions, "
    "verifier gaps, or environment quirks. The real agent sees only its `/app` "
    "workspace plus its prompt — probe-only material is staged separately under "
    "`/probe-harness/`, and a hidden answer key or reference solution there "
    "(tests/, solution/) is by design, not a leak, unless that same answer is "
    "reachable from `/app`. Be concise and concrete."
)


NOP_ORACLE_QUEUE_KEY = "nop_oracle"
_NOP_ORACLE_AGENTS: set[str] = {AgentName.NOP.value, AgentName.ORACLE.value}
OPENAI_PROVIDER_AZURE = "azure"
OPENAI_PROVIDER_OPENAI = "openai"
_OPENAI_PROVIDERS: set[str] = {OPENAI_PROVIDER_AZURE, OPENAI_PROVIDER_OPENAI}

# Cross-region inference profile prefixes used for AWS Bedrock model ids, e.g.
# "global.anthropic.claude-haiku-4-5-20251001-v1:0".
_BEDROCK_REGION_PREFIXES: tuple[str, ...] = ("us.", "eu.", "apac.", "apn.", "global.")

# Environment variables that put Claude Code into Bedrock mode. The Modal image
# sets these globally so Bedrock is the default route for Oddish-run Claude jobs.
BEDROCK_ENV_VARS: tuple[str, ...] = (
    "AWS_BEARER_TOKEN_BEDROCK",
    "CLAUDE_CODE_USE_BEDROCK",
)

# z.ai / GLM routing. Z.ai's GLM models are served over an Anthropic-compatible
# /messages endpoint, so they run on the claude-code harness -- but they must
# NOT be bucketed under the Bedrock provider/queue (claude-code's fixed default
# provider) or they would contend with heavy Bedrock/Anthropic traffic for the
# same concurrency slots. We canonicalize every GLM/z.ai reference to a
# ``zai/<id>`` id so provider detection, queue keys, and Harbor's per-agent
# network allowlist all resolve to z.ai instead of falling through to Bedrock.
ZAI_PROVIDER = "zai"
ZAI_DEFAULT_BASE_URL = "https://api.z.ai/api/anthropic"
# Provider prefixes that mean "this is a z.ai/GLM model". Canonicalized to
# ``zai`` (the litellm provider id, which Harbor's allowlist also recognizes).
_ZAI_PROVIDER_PREFIXES: frozenset[str] = frozenset({"zai", "z-ai", "z.ai"})


def is_zai_model(model: str | None) -> bool:
    """Return True if *model* should route to z.ai's GLM endpoint.

    Matches an explicit ``zai/``/``z-ai/``/``z.ai/`` provider prefix or a bare
    ``glm...`` model id (e.g. ``glm-x-preview[1m]``, ``glm-4.6``).
    """
    if not model:
        return False
    raw = model.strip().lower()
    if not raw:
        return False
    provider_prefix, bare = split_provider_model_name(raw)
    if provider_prefix and provider_prefix.strip().lower() in _ZAI_PROVIDER_PREFIXES:
        return True
    tail = bare if provider_prefix else raw
    return tail.split("/")[-1].startswith("glm")


def zai_bare_model_id(model: str) -> str:
    """Strip any z.ai provider prefix, returning the bare GLM model id.

    ``zai/glm-x-preview[1m]`` -> ``glm-x-preview[1m]``; a bare id is returned
    unchanged. This is the id Claude Code must send as ``ANTHROPIC_MODEL``.
    """
    raw = model.strip()
    provider_prefix, bare = split_provider_model_name(raw)
    if provider_prefix and provider_prefix.strip().lower() in _ZAI_PROVIDER_PREFIXES:
        return bare.strip()
    return raw


def to_zai_model_id(model: str | None) -> str | None:
    """Canonicalize a GLM/z.ai reference to ``zai/<bare-id>``.

    Non-z.ai models are returned unchanged. This is the single chokepoint that
    keeps GLM trials off the Bedrock provider/queue: the ``zai`` prefix is a
    recognized litellm provider, so downstream provider detection and queue-key
    derivation resolve to ``zai`` instead of claude-code's fixed Bedrock
    fallback, and Harbor's network allowlist maps the ``zai`` prefix to
    ``api.z.ai``.
    """
    if not is_zai_model(model):
        return model
    assert model is not None
    return f"{ZAI_PROVIDER}/{zai_bare_model_id(model)}"


# MiniMax / Moonshot (Kimi) routing. Like GLM/z.ai, these are served over an
# Anthropic-compatible /messages endpoint and run on the claude-code harness,
# but must NOT inherit claude-code's fixed Bedrock provider/queue. We
# canonicalize each reference to ``<provider>/<id>`` so provider detection,
# queue keys, and Harbor's per-agent network allowlist resolve to the direct
# provider endpoint instead of Bedrock.
MINIMAX_PROVIDER = "minimax"
MINIMAX_DEFAULT_BASE_URL = "https://api.minimax.io/anthropic"
# An explicit ``minimax/`` prefix or a bare ``minimax...`` model id (e.g.
# ``MiniMax-M3``) routes to MiniMax direct.
_MINIMAX_PROVIDER_PREFIXES: frozenset[str] = frozenset({"minimax"})
# MiniMax publishes mixed-case ids; oddish lowercases every model id for
# storage/queueing, so re-case the known ids to what the MiniMax endpoint
# expects when handing the id to Claude Code.
_MINIMAX_API_MODEL_IDS: dict[str, str] = {"minimax-m3": "MiniMax-M3"}

MOONSHOT_PROVIDER = "moonshot"
MOONSHOT_DEFAULT_BASE_URL = "https://api.moonshot.ai/anthropic"
# An explicit ``moonshot/``/``moonshotai/``/``kimi/`` prefix or a truly bare
# ``kimi-...`` id routes to Moonshot direct. A foreign provider prefix
# (``openrouter/moonshotai/kimi-...``) is intentionally NOT matched so the
# OpenRouter route keeps its own provider/queue bucket.
_MOONSHOT_PROVIDER_PREFIXES: frozenset[str] = frozenset(
    {"moonshot", "moonshotai", "kimi"}
)


def is_minimax_model(model: str | None) -> bool:
    """Return True if *model* should route to MiniMax's direct endpoint."""
    if not model:
        return False
    raw = model.strip().lower()
    if not raw:
        return False
    provider_prefix, bare = split_provider_model_name(raw)
    if provider_prefix:
        return provider_prefix.strip().lower() in _MINIMAX_PROVIDER_PREFIXES
    return raw.startswith("minimax")


def minimax_bare_model_id(model: str) -> str:
    """Strip any MiniMax provider prefix, returning the bare model id."""
    raw = model.strip()
    provider_prefix, bare = split_provider_model_name(raw)
    if (
        provider_prefix
        and provider_prefix.strip().lower() in _MINIMAX_PROVIDER_PREFIXES
    ):
        return bare.strip()
    return raw


def minimax_api_model_id(bare_model_id: str) -> str:
    """Re-case a bare MiniMax id to the exact id the endpoint expects."""
    return _MINIMAX_API_MODEL_IDS.get(bare_model_id.strip().lower(), bare_model_id)


def to_minimax_model_id(model: str | None) -> str | None:
    """Canonicalize a MiniMax reference to ``minimax/<bare-id>``."""
    if not is_minimax_model(model):
        return model
    assert model is not None
    return f"{MINIMAX_PROVIDER}/{minimax_bare_model_id(model)}"


def is_moonshot_model(model: str | None) -> bool:
    """Return True if *model* should route to Moonshot's direct endpoint.

    Matches an explicit ``moonshot``/``moonshotai``/``kimi`` provider prefix or
    a truly bare ``kimi-...`` model id. A foreign provider prefix such as
    ``openrouter/`` is not matched, so OpenRouter-routed Kimi keeps its own
    routing.
    """
    if not model:
        return False
    raw = model.strip().lower()
    if not raw:
        return False
    provider_prefix, bare = split_provider_model_name(raw)
    if provider_prefix:
        return provider_prefix.strip().lower() in _MOONSHOT_PROVIDER_PREFIXES
    return raw.startswith("kimi-")


def moonshot_bare_model_id(model: str) -> str:
    """Strip any Moonshot/Kimi provider prefix, returning the bare model id."""
    raw = model.strip()
    provider_prefix, bare = split_provider_model_name(raw)
    if (
        provider_prefix
        and provider_prefix.strip().lower() in _MOONSHOT_PROVIDER_PREFIXES
    ):
        return bare.strip()
    return raw


def to_moonshot_model_id(model: str | None) -> str | None:
    """Canonicalize a Moonshot/Kimi reference to ``moonshot/<bare-id>``."""
    if not is_moonshot_model(model):
        return model
    assert model is not None
    return f"{MOONSHOT_PROVIDER}/{moonshot_bare_model_id(model)}"


def looks_like_bedrock_model_id(model: str | None) -> bool:
    """Return True if *model* is a Bedrock-style id that should route through AWS.

    Handles the three shapes AWS Bedrock accepts:
      * ARNs: ``arn:aws:bedrock:...``
      * Native ids: ``anthropic.claude-...``
      * Cross-region inference profiles: ``us.anthropic.claude-...``
    """
    if not model:
        return False
    tail = model.split("/", 1)[-1].strip().lower()
    if not tail:
        return False
    if tail.startswith("arn:aws:bedrock:"):
        return True
    if tail.startswith("anthropic."):
        return True
    if any(tail.startswith(p) for p in _BEDROCK_REGION_PREFIXES) and (
        ".anthropic." in tail
    ):
        return True
    return False


# Anthropic-style Claude model ids mapped to their invokable AWS Bedrock ids.
# oddish runs Claude exclusively through AWS Bedrock. Claude Code invokes
# Bedrock via the legacy InvokeModel API, which only accepts cross-region
# inference profile ids (a "global."/"us."/... prefix) or ARNs — bare
# "anthropic.claude-..." foundation-model ids are NOT invokable on-demand.
# So every value below is a "global." inference profile id, except the two
# legacy Opus models that have no global profile (they use "us.").
#
# Keys are the lowercased model id with any "provider/" prefix removed (e.g.
# "anthropic/claude-haiku-4-5" and bare "claude-haiku-4-5" both look up
# "claude-haiku-4-5"); both the dated Claude API id and its dateless alias
# are listed where they differ. An unmapped Claude id raises in
# to_bedrock_model_id() rather than reaching Bedrock as an uninvokable id.
#
# Sources:
#   https://platform.claude.com/docs/en/about-claude/models/overview
#   https://platform.claude.com/docs/en/build-with-claude/claude-on-amazon-bedrock-legacy
_ANTHROPIC_TO_BEDROCK_MODEL_IDS: dict[str, str] = {
    # Current models
    #
    # Fable 5 is a Covered Model: Bedrock only serves it once the AWS
    # account's data retention mode is set to "provider_data_share" (a
    # one-time `PUT /data-retention` opt-in; API-only, no console UI).
    # Without it, Bedrock rejects every call with "data retention mode
    # 'default' is not available for this model".
    "claude-fable-5": "global.anthropic.claude-fable-5",
    "claude-opus-4-8": "global.anthropic.claude-opus-4-8",
    "claude-sonnet-4-6": "global.anthropic.claude-sonnet-4-6",
    "claude-haiku-4-5": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "claude-haiku-4-5-20251001": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    # Legacy models
    "claude-opus-4-7": "global.anthropic.claude-opus-4-7",
    "claude-opus-4-6": "global.anthropic.claude-opus-4-6-v1",
    "claude-sonnet-4-5": "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "claude-sonnet-4-5-20250929": "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "claude-opus-4-5": "global.anthropic.claude-opus-4-5-20251101-v1:0",
    "claude-opus-4-5-20251101": "global.anthropic.claude-opus-4-5-20251101-v1:0",
    # Opus 4.1 / Opus 4 have no "global." inference profile — use "us.".
    "claude-opus-4-1": "us.anthropic.claude-opus-4-1-20250805-v1:0",
    "claude-opus-4-1-20250805": "us.anthropic.claude-opus-4-1-20250805-v1:0",
    "claude-sonnet-4-0": "global.anthropic.claude-sonnet-4-20250514-v1:0",
    "claude-sonnet-4-20250514": "global.anthropic.claude-sonnet-4-20250514-v1:0",
    "claude-opus-4-0": "us.anthropic.claude-opus-4-20250514-v1:0",
    "claude-opus-4-20250514": "us.anthropic.claude-opus-4-20250514-v1:0",
}


def to_bedrock_model_id(model: str | None) -> str | None:
    """Normalize any Claude model reference to an invokable AWS Bedrock id.

    oddish routes Claude exclusively through AWS Bedrock. Claude Code invokes
    Bedrock via the legacy InvokeModel API, which only accepts ids that are
    directly invokable: ARNs and cross-region inference profile ids
    (``global.``/``us.``/``eu.``/... prefixed). Bare ``anthropic.claude-...``
    foundation-model ids are NOT invokable on-demand, so they get re-resolved
    through the mapping table like any other Claude reference.

    This is the single chokepoint that guarantees whatever reaches Claude Code
    is an invokable Bedrock id:

      * ``None`` / blank -> returned unchanged
      * non-Claude models (``openai/...``, ``gemini-...``) -> returned unchanged
      * an explicit non-Anthropic provider prefix (``openrouter/...``, etc.) ->
        returned unchanged so it runs through that provider, even when the rest
        of the id mentions Claude (``openrouter/anthropic/claude-opus-4.8``)
      * ARNs and inference-profile ids -> returned as-is (minus any leading
        ``bedrock/`` prefix)
      * everything else containing "claude" (``anthropic/claude-...``, bare
        ``claude-...``, bare ``anthropic.claude-...``) -> mapped via
        ``_ANTHROPIC_TO_BEDROCK_MODEL_IDS``

    Raises ``ValueError`` for a Claude model id with no Bedrock mapping rather
    than silently handing Bedrock an id it cannot invoke.
    """
    if model is None:
        return None
    stripped = model.strip()
    if not stripped:
        return model

    # Drop a redundant "bedrock/" prefix (bedrock/us.anthropic.* -> us.anthropic.*).
    if stripped.lower().startswith("bedrock/"):
        stripped = stripped.split("/", 1)[1]
    lowered = stripped.lower()

    # ARNs and cross-region inference profile ids are already invokable as-is.
    if lowered.startswith("arn:aws:bedrock:"):
        return stripped
    if any(lowered.startswith(p) for p in _BEDROCK_REGION_PREFIXES) and (
        ".anthropic." in lowered
    ):
        return stripped

    # An explicit non-Anthropic provider prefix means the caller has chosen a
    # specific transport (e.g. "openrouter/anthropic/claude-opus-4.8" must run
    # through OpenRouter, not Bedrock). Honor it and pass the id through; only
    # bare Claude ids and the "anthropic/"/"claude/" routes get Bedrock-mapped.
    provider_prefix, _ = split_provider_model_name(stripped)
    if provider_prefix and provider_prefix.strip().lower() not in {
        "anthropic",
        "claude",
    }:
        return stripped

    # Resolve everything else through the table, keyed by the lowercased id
    # with any "provider/" prefix removed. Non-Claude models route through
    # their own providers untouched.
    key = stripped.split("/", 1)[-1].strip().lower()
    if "claude" not in key:
        return stripped

    # Bare Bedrock foundation-model ids (anthropic.claude-...-v1:0) are not
    # invokable on-demand; reduce them to the table's Anthropic-style key.
    if key.startswith("anthropic."):
        key = key[len("anthropic.") :]
        for version_suffix in ("-v1:0", "-v1"):
            if key.endswith(version_suffix):
                key = key[: -len(version_suffix)]
                break

    bedrock_id = _ANTHROPIC_TO_BEDROCK_MODEL_IDS.get(key)
    if bedrock_id is None:
        raise ValueError(
            f"No Bedrock model id mapping for Claude model {model!r}. "
            "oddish runs Claude through AWS Bedrock only — add an entry to "
            "_ANTHROPIC_TO_BEDROCK_MODEL_IDS in oddish.config."
        )
    return bedrock_id


# Reverse of _ANTHROPIC_TO_BEDROCK_MODEL_IDS, used to route a model back to the
# direct Anthropic API. Several Anthropic ids (a dated alias and its dateless
# form) map to one Bedrock id; prefer the shorter, dateless alias so callers get
# the canonical API id (e.g. "claude-haiku-4-5", not "claude-haiku-4-5-20251001").
_BEDROCK_TO_ANTHROPIC_MODEL_IDS: dict[str, str] = {}
for _anthropic_id, _bedrock_id in _ANTHROPIC_TO_BEDROCK_MODEL_IDS.items():
    _existing = _BEDROCK_TO_ANTHROPIC_MODEL_IDS.get(_bedrock_id)
    if _existing is None or len(_anthropic_id) < len(_existing):
        _BEDROCK_TO_ANTHROPIC_MODEL_IDS[_bedrock_id] = _anthropic_id
del _anthropic_id, _bedrock_id, _existing


def to_anthropic_api_model_id(model: str | None) -> str | None:
    """Resolve a Claude model reference to its direct Anthropic API id.

    The practical inverse of ``to_bedrock_model_id``: a Bedrock inference-profile
    id (``global.anthropic.claude-haiku-4-5-20251001-v1:0``) maps back to the
    plain API id (``claude-haiku-4-5``). Used by callers that run on the direct
    Anthropic API (``ANTHROPIC_API_KEY``) rather than Bedrock -- e.g. the probe
    summary analyzer. Plain Claude ids keep their value (minus an
    ``anthropic/``/``claude/`` provider prefix); non-Claude ids pass through.
    """
    if model is None:
        return None
    stripped = model.strip()
    if not stripped:
        return model

    # Drop a redundant "bedrock/" transport prefix before matching.
    if stripped.lower().startswith("bedrock/"):
        stripped = stripped.split("/", 1)[1]

    # Known Bedrock inference-profile / foundation-model id -> plain API id.
    mapped = _BEDROCK_TO_ANTHROPIC_MODEL_IDS.get(stripped.lower())
    if mapped:
        return mapped

    # Strip an "anthropic/"/"claude/" provider prefix to expose a bare API id;
    # any other provider prefix is a deliberate transport choice -- pass through.
    provider_prefix, bare = split_provider_model_name(stripped)
    if provider_prefix and provider_prefix.strip().lower() in {"anthropic", "claude"}:
        return bare
    return stripped


def _to_bedrock_model_id_if_known(model: str) -> str:
    """Best-effort Bedrock canonicalization for read-side legacy metadata.

    New trial creation calls ``to_bedrock_model_id`` through
    ``normalize_trial_model`` and remains strict. Queue/admin/dashboard reads
    may encounter historical queue keys with unmapped Claude aliases; those
    should remain visible instead of breaking the whole response.
    """
    try:
        return to_bedrock_model_id(model) or model
    except ValueError:
        return model


def normalize_model_id(model: str | None) -> str | None:
    """Canonicalize model identifiers for storage and display.

    Model IDs should be lowercase, preserve provider prefixes, and avoid
    whitespace-only variants that would fragment usage aggregation.
    """
    if model is None:
        return None

    stripped = model.strip().lower()
    if not stripped:
        return None

    normalized_parts: list[str] = []
    for part in stripped.split("/"):
        normalized_part = re.sub(r"\s+", "-", part.strip())
        normalized_part = re.sub(r"-{2,}", "-", normalized_part).strip("-")
        if normalized_part:
            normalized_parts.append(normalized_part)

    if not normalized_parts:
        return None

    normalized = "/".join(normalized_parts)
    if normalized in _MODEL_ABSENT_ALIASES:
        return None
    return normalized


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
    # Claude transports. Oddish-run Claude trials canonicalize to Bedrock, while
    # direct Anthropic ids can still appear in imported/off-platform data.
    "anthropic": "anthropic",
    "claude": "anthropic",
    "bedrock": "bedrock",
    # Gemini / Google
    "gemini": "gemini",
    "google": "gemini",
    "vertex_ai": "gemini",
    "palm": "gemini",
    # z.ai / GLM. All spellings collapse to the canonical "zai" provider so
    # GLM trials get their own queue/provider bucket instead of Bedrock's.
    "zai": ZAI_PROVIDER,
    "z-ai": ZAI_PROVIDER,
    "z.ai": ZAI_PROVIDER,
    "glm": ZAI_PROVIDER,
    # MiniMax / Moonshot (Kimi). Same idea: direct-API models get their own
    # provider/queue bucket instead of Bedrock's.
    "minimax": MINIMAX_PROVIDER,
    "moonshot": MOONSHOT_PROVIDER,
    "moonshotai": MOONSHOT_PROVIDER,
    "kimi": MOONSHOT_PROVIDER,
}


def _normalize_model_provider(provider: str) -> str | None:
    normalized = provider.strip().lower()
    if not normalized:
        return None
    if normalized in _MODEL_PROVIDER_ALIASES:
        return _MODEL_PROVIDER_ALIASES[normalized]
    if normalized in PROVIDER_KEYS:
        return normalized
    return None


def _get_provider_from_model(model_name: str) -> str | None:
    if looks_like_bedrock_model_id(model_name):
        return "bedrock"
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
    if lowered.startswith("glm"):
        return ZAI_PROVIDER
    if lowered.startswith("minimax"):
        return MINIMAX_PROVIDER
    if lowered.startswith("kimi-"):
        return MOONSHOT_PROVIDER

    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Load .env first, then layer .env.local over it (later file wins on
        # duplicate keys). Both are resolved relative to the process CWD, so a
        # local backend run from backend/ picks up backend/.env and
        # backend/.env.local automatically; in Modal containers neither file
        # exists, so the entries are no-ops and config comes from real env vars.
        # Exported process env vars still outrank both files.
        env_file=(".env", ".env.local"),
        env_prefix="ODDISH_",
        extra="ignore",
    )

    # ==========================================================================
    # Defaults — all configurable via ODDISH_<FIELD> env vars
    # ==========================================================================

    # Worker behavior
    auto_start_workers: bool = True

    # Local dev: dispatch trials to the in-process runner
    # (``worker.local_runner``) instead of the Modal/cloud queue. Set
    # ODDISH_LOCAL_MODE=1 to exercise probe trials end-to-end on a dev box.
    local_mode: bool = False

    # Local execution scratch paths
    harbor_jobs_dir: str = "/tmp/harbor-jobs"

    # Default execution environment (daytona, docker, or modal)
    harbor_environment: str = "daytona"

    # Daytona sandbox auto-cleanup safety net (minutes). A sandbox idle
    # (no SDK events) for ``daytona_auto_stop_interval_mins`` is stopped;
    # once stopped for ``daytona_auto_delete_interval_mins`` it is deleted.
    # This is the backstop for sandboxes that escape explicit teardown via
    # ``cancel_job_by_worker``; 0 disables auto-stop, so keep it positive.
    daytona_auto_stop_interval_mins: int = 30
    daytona_auto_delete_interval_mins: int = 60

    # Our Daytona region only permits ephemeral sandboxes -- ``daytona.create``
    # rejects persistent ones with "Only ephemeral sandboxes are permitted in
    # this region". Ephemeral sandboxes auto-delete when stopped, so harbor
    # forces ``auto_delete_interval`` to 0 under this flag; the auto-stop above
    # still applies as the idle backstop.
    daytona_ephemeral: bool = True

    # API server
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Database connection pools (constants — override on Settings class
    # in entry modules for different deployment targets)
    db_use_null_pool: ClassVar[bool] = False
    db_pool_max_overflow: ClassVar[int] = 10
    db_pool_size: ClassVar[int] = 5

    # Queue limits — use ODDISH_MODEL_CONCURRENCY_OVERRIDES for per-model
    # values and ODDISH_DEFAULT_MODEL_CONCURRENCY for fallback.
    default_model_concurrency: int = 8
    nop_oracle_concurrency: int = 32
    model_concurrency_overrides: dict[str, int] = Field(default_factory=dict)
    analysis_model: str = ANALYSIS_MODEL
    verdict_model: str = VERDICT_MODEL

    # Agent to provider mapping (computed from Harbor's AgentName enum)
    agent_to_provider: ClassVar[dict[str, str]] = _build_agent_provider_map()

    # ==========================================================================
    # ENV-VAR CONFIGURABLE - Secrets and infrastructure only
    # ==========================================================================

    # Database
    database_url: str = "postgresql+asyncpg://oddish:oddish@localhost:5432/oddish"

    # Asyncpg pool sizing
    # Defaults are intentionally small to avoid exhausting DB connections when
    # many worker processes are spawned.
    asyncpg_pool_min_size: int = 1
    asyncpg_pool_max_size: int = 4

    # Postgres safety net against orphaned transactions.
    #
    # When a Modal worker is killed mid-transaction (e.g. cancel API calling
    # terminate_containers=True), SIGKILL prevents Python from running any
    # rollback. The TCP connection dies, but a transaction-mode pooler
    # (Supavisor / PgBouncer) keeps the Postgres backend open and Postgres
    # sees the transaction as "idle in transaction" forever, holding row and
    # table locks that block heartbeat writes and DDL migrations.
    #
    # When we can, we ship this via server_settings so Postgres itself
    # aborts any transaction left idle this long. NOTE: Supavisor (Supabase)
    # currently drops client-supplied server_settings, so on Supabase this
    # setting only applies on direct (non-pooled) connections; on pooled
    # connections you need to run ALTER ROLE postgres SET
    # idle_in_transaction_session_timeout=... (see oddish.db.apply_role_defaults)
    # and rely on the reaper in cleanup as a backstop.
    idle_in_transaction_session_timeout_ms: int = 300_000
    # Advertised to pg_stat_activity.application_name. On Supabase this
    # ends up overwritten by Supavisor; we still set it because (a) it
    # works on direct connections and (b) the reaper also matches it.
    db_application_name: str = "oddish"
    # Application names that, when seen in pg_stat_activity, identify
    # connections the reaper is allowed to terminate. Matches either our
    # configured application_name (direct connections) or the transaction
    # pooler identity (Supavisor / PgBouncer) that rewrites it. Other
    # Supabase-native services use distinct names like 'postgrest',
    # 'Supabase Storage API Canary', 'pg_cron scheduler' and are never
    # matched here.
    db_reaper_application_names: list[str] = Field(
        default_factory=lambda: ["oddish", "Supavisor"]
    )

    @property
    def asyncpg_url(self) -> str:
        """Database URL without +asyncpg prefix."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://")

    def asyncpg_server_settings(self) -> dict[str, str]:
        """Postgres session GUCs to apply to every asyncpg connection."""
        return {
            "application_name": self.db_application_name,
            "idle_in_transaction_session_timeout": str(
                self.idle_in_transaction_session_timeout_ms
            ),
        }

    # S3-compatible storage (required)
    s3_endpoint_url: str | None = None
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "data"
    s3_region: str = "us-east-1"

    # Sauron S3 mirror (optional, disabled when bucket is empty).
    # When configured, oddish workers also upload trial artifacts to sauron's
    # AWS S3 bucket in sauron's expected directory layout, allowing sauron's
    # frontend to render oddish-originated experiments natively.
    # Uses AWS_ACCESS_KEY_ID/SECRET_ACCESS_KEY from environment for credentials.
    sauron_s3_bucket: str = ""
    # Org slug used as the top-level path segment for non-PR (CLI-triggered)
    # experiments. PR-triggered runs derive owner/repo from task.tags.github_meta.
    sauron_s3_org: str = "oddish"

    # Task archive expansion (derived per-file layout for fast listings).
    # When enabled, uploading a new task version enqueues a
    # ``TASK_EXPAND`` worker job that writes the tarball's contents out
    # as individual S3 objects under ``tasks/{task_id}/v{N}-files/``
    # alongside a ``.oddish-manifest.json`` sentinel. The canonical
    # archive at ``tasks/{task_id}/v{N}/.oddish-task.tar.gz`` is never
    # touched, so runner download paths remain unchanged.
    tasks_expand_archive: bool = True
    tasks_expand_max_bytes: int = 1_073_741_824  # 1 GiB
    tasks_expand_max_member_bytes: int = 104_857_600  # 100 MiB
    # Per-process in-memory cache for downloaded task archives, keyed by
    # ``(archive_key, etag)``. Covers the archive fallback read path so
    # pre-expansion versions and legacy tasks don't re-download the
    # tarball on every click.
    tasks_archive_cache_mb: int = 256

    # OpenAI-family routing. Azure is the enterprise default; public OpenAI
    # requires explicitly setting ODDISH_OPENAI_PROVIDER=openai.
    openai_provider: str = OPENAI_PROVIDER_AZURE

    # API keys (read from env without ODDISH_ prefix)
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
    azure_openai_api_key: str | None = Field(default=None, alias="AZURE_OPENAI_API_KEY")
    azure_openai_endpoint: str | None = Field(
        default=None, alias="AZURE_OPENAI_ENDPOINT"
    )
    azure_openai_api_version: str | None = Field(
        default=None, alias="AZURE_OPENAI_API_VERSION"
    )
    azure_openai_deployments: dict[str, str] = Field(default_factory=dict)
    # Deprecated compatibility field. Runtime routing should use
    # ODDISH_AZURE_OPENAI_DEPLOYMENTS so each requested model maps to an
    # explicit Azure deployment.
    azure_openai_deployment: str | None = Field(
        default=None, alias="AZURE_OPENAI_DEPLOYMENT"
    )

    # ==========================================================================
    # Helper methods
    # ==========================================================================

    @model_validator(mode="after")
    def normalize_model_overrides(self) -> "Settings":
        raw = os.getenv("ODDISH_MODEL_CONCURRENCY_OVERRIDES")
        if raw:
            try:
                parsed = json.loads(raw)
            except Exception as exc:
                raise ValueError(
                    "ODDISH_MODEL_CONCURRENCY_OVERRIDES must be valid JSON"
                ) from exc
            if not isinstance(parsed, dict):
                raise ValueError(
                    "ODDISH_MODEL_CONCURRENCY_OVERRIDES must be a JSON object"
                )
            normalized: dict[str, int] = {}
            for key, value in parsed.items():
                queue_key = self.normalize_queue_key(str(key))
                normalized[queue_key] = int(value)
            self.model_concurrency_overrides = normalized

        self.azure_openai_deployments = self._normalize_azure_openai_deployments(
            self.azure_openai_deployments
        )
        return self

    @staticmethod
    def _normalize_azure_openai_deployments(
        deployments: dict[str, str],
    ) -> dict[str, str]:
        normalized: dict[str, str] = {}
        if not isinstance(deployments, dict):
            raise ValueError("ODDISH_AZURE_OPENAI_DEPLOYMENTS must be a JSON object")
        for key, value in deployments.items():
            model_key = normalize_model_id(str(key))
            deployment = str(value).strip()
            if not model_key or not deployment:
                continue
            normalized[model_key] = deployment
        return normalized

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
        - Canonicalize Claude models to their Bedrock runtime id, since Oddish
          runs Claude through Bedrock and persists the same id it executes.
        - Otherwise return cleaned model (or None if missing).
        """
        cleaned = normalize_model_id(model)

        normalized_agent = (agent or "").strip().lower()
        if normalized_agent in _NOP_ORACLE_AGENTS:
            return "default"

        # GLM/z.ai, MiniMax, and Moonshot/Kimi models run on the claude-code
        # harness but route to their own direct endpoints, not Bedrock.
        # Canonicalize to "<provider>/<id>" before the Bedrock chokepoint so
        # they get their own provider/queue bucket instead of claude-code's
        # fixed Bedrock fallback.
        if is_zai_model(cleaned):
            return to_zai_model_id(cleaned)
        if is_minimax_model(cleaned):
            return to_minimax_model_id(cleaned)
        if is_moonshot_model(cleaned):
            return to_moonshot_model_id(cleaned)

        return to_bedrock_model_id(cleaned)

    def normalize_queue_key(self, model: str) -> str:
        """Normalize queue keys.

        Claude aliases collapse to the same Bedrock id that is persisted on the
        trial, so queueing/concurrency and execution use one model id. For other
        bare model inputs, infer a provider prefix as before.
        """
        normalized = model.strip().lower().replace(" ", "_")
        if not normalized or normalized in _MODEL_ABSENT_ALIASES:
            return "default"
        if normalized in _PROVIDER_ONLY_QUEUE_ALIASES:
            return "default"
        normalized = _to_bedrock_model_id_if_known(normalized)
        if looks_like_bedrock_model_id(normalized):
            return normalized
        if "/" in normalized:
            provider_prefix, canonical = normalized.split("/", 1)
            if (
                provider_prefix in _PROVIDER_ONLY_QUEUE_ALIASES
                and canonical in _PROVIDER_ONLY_QUEUE_ALIASES
            ):
                return "default"
            return normalized

        inferred_prefix = _infer_provider_prefix(normalized)
        if not inferred_prefix:
            return normalized
        return f"{inferred_prefix}/{normalized}"

    def get_queue_key_for_trial(self, agent: str, model: str | None) -> str:
        """Resolve queue key from model first, fallback to provider bucket."""
        normalized_agent = (agent or "").strip().lower()
        if normalized_agent in _NOP_ORACLE_AGENTS:
            return NOP_ORACLE_QUEUE_KEY
        normalized_model = self.normalize_trial_model(agent, model)
        if normalized_model:
            return self.normalize_queue_key(normalized_model)
        return "default"

    def get_analysis_queue_key(self) -> str:
        return self.normalize_queue_key(self.analysis_model)

    def get_verdict_queue_key(self) -> str:
        return self.normalize_queue_key(self.verdict_model)

    def get_task_expand_queue_key(self) -> str:
        """Dedicated queue key for task-expansion jobs.

        Expansion is I/O bound against S3 rather than LLM-rate-limited, so
        a plain literal queue key is fine; it still benefits from the
        per-queue-key concurrency leases that gate every other kind.
        """
        return "task_expand"

    def get_model_concurrency(self, queue_key: str) -> int:
        normalized = self.normalize_queue_key(queue_key)
        override = self.model_concurrency_overrides.get(normalized)
        if override is not None:
            return max(int(override), 0)
        if normalized == NOP_ORACLE_QUEUE_KEY:
            return max(int(self.nop_oracle_concurrency), 0)
        return max(int(self.default_model_concurrency), 0)

    def get_known_queue_keys(self) -> set[str]:
        keys = {
            NOP_ORACLE_QUEUE_KEY,
            self.get_analysis_queue_key(),
            self.get_verdict_queue_key(),
        }
        keys.update(self.model_concurrency_overrides.keys())
        return keys

    def get_openai_provider(self) -> str:
        provider = self.openai_provider.strip().lower()
        if provider not in _OPENAI_PROVIDERS:
            allowed = ", ".join(sorted(_OPENAI_PROVIDERS))
            raise ValueError(
                f"ODDISH_OPENAI_PROVIDER must be one of: {allowed}. "
                f"Got {self.openai_provider!r}."
            )
        return provider

    def get_public_openai_warning(self) -> str:
        return (
            "ODDISH_OPENAI_PROVIDER=openai routes OpenAI-family jobs to the "
            "public OpenAI API. Azure OpenAI is the default for enterprise "
            "deployments."
        )

    def require_azure_openai_config(self) -> dict[str, str]:
        missing = [
            name
            for name, value in {
                "AZURE_OPENAI_API_KEY": self.azure_openai_api_key,
                "AZURE_OPENAI_ENDPOINT": self.azure_openai_endpoint,
                "AZURE_OPENAI_API_VERSION": self.azure_openai_api_version,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Azure OpenAI is the default OpenAI-family provider. "
                f"Set {', '.join(missing)} or explicitly set "
                "ODDISH_OPENAI_PROVIDER=openai to use the public OpenAI API."
            )
        return {
            "api_key": self.azure_openai_api_key or "",
            "endpoint": self.azure_openai_endpoint or "",
            "api_version": self.azure_openai_api_version or "",
        }

    def resolve_azure_openai_deployment(self, model: str | None) -> str:
        normalized = normalize_model_id(model)
        if not normalized:
            raise ValueError(
                "Azure OpenAI routing requires an OpenAI model id. Set a model "
                "such as 'openai/gpt-5.2' and add it to "
                "ODDISH_AZURE_OPENAI_DEPLOYMENTS."
            )

        lookup_keys = [normalized]
        if normalized.startswith("openai/"):
            lookup_keys.append(normalized.split("/", 1)[1])
        elif "/" not in normalized:
            lookup_keys.append(f"openai/{normalized}")

        for key in lookup_keys:
            deployment = self.azure_openai_deployments.get(key)
            if deployment:
                return deployment

        examples = "', '".join(lookup_keys)
        raise ValueError(
            f"No Azure OpenAI deployment mapping for OpenAI model {normalized!r}. "
            "Set ODDISH_AZURE_OPENAI_DEPLOYMENTS to a JSON object with a key "
            f"for '{examples}'."
        )

    def get_azure_openai_base_url(self) -> str:
        """Return the OpenAI-compatible Azure OpenAI v1 base URL.

        Foundry project endpoints are for project/agent APIs. Oddish Harbor
        jobs use the OpenAI SDK path, so configure the Azure OpenAI endpoint
        shown for the deployment, typically ``*.openai.azure.com/openai/v1``.
        """
        azure = self.require_azure_openai_config()
        endpoint = azure["endpoint"].rstrip("/")
        if "/api/projects/" in endpoint:
            raise RuntimeError(
                "AZURE_OPENAI_ENDPOINT must be the OpenAI-compatible Azure "
                "OpenAI endpoint, such as "
                "'https://YOUR-RESOURCE.openai.azure.com/openai/v1'. "
                "Do not use the Foundry project endpoint "
                "'https://YOUR-RESOURCE.services.ai.azure.com/api/projects/...'."
            )
        if endpoint.endswith("/openai/v1"):
            return endpoint
        if "/openai/" in endpoint:
            return endpoint
        return f"{endpoint}/openai/v1"

    def require_public_openai_config(
        self, api_key: str | None = None
    ) -> dict[str, str]:
        key = api_key or self.openai_api_key
        if not key:
            raise RuntimeError(
                "OPENAI_API_KEY is required when "
                "ODDISH_OPENAI_PROVIDER=openai. Azure OpenAI is the default; "
                "set AZURE_OPENAI_* values to use Azure instead."
            )
        return {"api_key": key}

    def get_openai_runtime_env(
        self, *, model: str | None = None, api_key: str | None = None
    ) -> dict[str, str]:
        """Return process env vars for OpenAI-family provider clients.

        In Azure mode this intentionally does not set ``OPENAI_API_KEY``.
        If a downstream tool ignores Azure endpoint variables, failing closed is
        safer than sending task data to the public OpenAI API with an Azure key.
        """
        if self.get_openai_provider() == OPENAI_PROVIDER_OPENAI:
            public = self.require_public_openai_config(api_key=api_key)
            return {"OPENAI_API_KEY": public["api_key"]}

        azure = self.require_azure_openai_config()
        deployment = self.resolve_azure_openai_deployment(model)
        return {
            "AZURE_OPENAI_API_KEY": azure["api_key"],
            "AZURE_OPENAI_ENDPOINT": azure["endpoint"],
            "AZURE_OPENAI_API_VERSION": azure["api_version"],
            "AZURE_OPENAI_DEPLOYMENT": deployment,
            # The OpenAI Python SDK reads OPENAI_API_VERSION for Azure clients;
            # keep the Azure-prefixed name too for tools that prefer it.
            "OPENAI_API_VERSION": azure["api_version"],
            "OPENAI_API_TYPE": "azure",
        }

    def get_openai_agent_env(
        self, *, model: str | None = None, api_key: str | None = None
    ) -> dict[str, str]:
        """Return env vars for OpenAI-family Harbor agents."""
        if self.get_openai_provider() == OPENAI_PROVIDER_OPENAI:
            return self.get_openai_runtime_env(api_key=api_key)

        azure = self.require_azure_openai_config()
        deployment = self.resolve_azure_openai_deployment(model)
        base_url = self.get_azure_openai_base_url()
        return {
            # Codex CLI expects OpenAI-compatible names and writes these into
            # its sandbox-local auth/config files.
            "OPENAI_API_KEY": azure["api_key"],
            "OPENAI_BASE_URL": base_url,
            # Harbor/LiteLLM-style Azure names for agents that support the
            # explicit azure provider route.
            "AZURE_API_KEY": azure["api_key"],
            "AZURE_API_BASE": base_url,
            "AZURE_API_VERSION": azure["api_version"],
            # Azure OpenAI SDK-style names for agent implementations that use
            # the official Python client directly.
            "AZURE_OPENAI_API_KEY": azure["api_key"],
            "AZURE_OPENAI_ENDPOINT": azure["endpoint"],
            "AZURE_OPENAI_API_VERSION": azure["api_version"],
            "AZURE_OPENAI_DEPLOYMENT": deployment,
            "OPENAI_API_VERSION": azure["api_version"],
            "OPENAI_API_TYPE": "azure",
        }


settings = Settings()
