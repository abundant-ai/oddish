from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import (
    NOP_ORACLE_QUEUE_KEY,
    Settings,
    normalize_model_id,
    to_anthropic_api_model_id,
)  # noqa: E402


def _settings(monkeypatch, *, clear_openai_env: bool = True, **kwargs) -> Settings:
    monkeypatch.delenv("ODDISH_MODEL_CONCURRENCY_OVERRIDES", raising=False)
    monkeypatch.delenv("ODDISH_NOP_ORACLE_CONCURRENCY", raising=False)
    if clear_openai_env:
        monkeypatch.delenv("ODDISH_OPENAI_PROVIDER", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
        monkeypatch.delenv("AZURE_OPENAI_API_VERSION", raising=False)
        monkeypatch.delenv("ODDISH_AZURE_OPENAI_DEPLOYMENTS", raising=False)
        monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT", raising=False)
    return Settings(_env_file=None, **kwargs)


def test_nop_and_oracle_use_single_id_for_model_and_queue(monkeypatch):
    settings = _settings(monkeypatch)

    # The model id and queue key must be the SAME single string so the stored
    # model, the queue key, and the concurrency bucket never drift apart.
    assert settings.normalize_trial_model("nop", None) == NOP_ORACLE_QUEUE_KEY
    assert settings.normalize_trial_model("oracle", None) == NOP_ORACLE_QUEUE_KEY
    assert settings.get_queue_key_for_trial("nop", None) == NOP_ORACLE_QUEUE_KEY
    assert settings.get_queue_key_for_trial("oracle", None) == NOP_ORACLE_QUEUE_KEY


def test_nop_oracle_variants_force_single_id(monkeypatch):
    settings = _settings(monkeypatch)

    # Suffixed / prefixed baseline variants must be treated like plain
    # nop/oracle: model and queue collapse to the one nop_oracle id, regardless
    # of whatever (often arbitrary) model string was passed.
    for agent in ("oracle-v2", "nop-baseline", "agent-nop", "agent-oracle-2"):
        for model in (None, "default", "nop_oracle", "some-random-thing"):
            assert (
                settings.normalize_trial_model(agent, model) == NOP_ORACLE_QUEUE_KEY
            ), (agent, model)
            assert (
                settings.get_queue_key_for_trial(agent, model) == NOP_ORACLE_QUEUE_KEY
            ), (agent, model)


def test_non_baseline_agents_are_not_treated_as_nop_oracle(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    # Substring matches that are not baseline variants must keep normal routing.
    assert settings.get_queue_key_for_trial("codex", "openai/gpt-5.2") == (
        "openai/gpt-5.2"
    )
    assert settings.normalize_trial_model("codex", "openai/gpt-5.2") == (
        "openai/gpt-5.2"
    )


def test_nop_oracle_queue_has_separate_default_concurrency(monkeypatch):
    settings = _settings(
        monkeypatch,
        default_model_concurrency=8,
        nop_oracle_concurrency=256,
    )

    assert settings.get_model_concurrency("default") == 8
    assert settings.get_model_concurrency(NOP_ORACLE_QUEUE_KEY) == 256
    assert NOP_ORACLE_QUEUE_KEY in settings.get_known_queue_keys()


def test_model_concurrency_overrides_can_override_nop_oracle_queue(monkeypatch):
    monkeypatch.setenv(
        "ODDISH_MODEL_CONCURRENCY_OVERRIDES",
        f'{{"{NOP_ORACLE_QUEUE_KEY}": 12, "default": 3}}',
    )
    settings = Settings(_env_file=None)

    assert settings.get_model_concurrency(NOP_ORACLE_QUEUE_KEY) == 12
    assert settings.get_model_concurrency("default") == 3


def test_claude_trial_model_is_persisted_as_bedrock_id(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    expected = "global.anthropic.claude-sonnet-4-6"

    assert (
        settings.normalize_trial_model("claude-code", "claude-sonnet-4-6") == expected
    )
    assert (
        settings.normalize_trial_model("claude-code", "bedrock/claude-sonnet-4-6")
        == expected
    )
    assert (
        settings.normalize_trial_model("claude-code", "claude/claude-sonnet-4-6")
        == expected
    )
    assert (
        settings.get_provider_for_trial("claude-code", "claude-sonnet-4-6") == "bedrock"
    )
    assert (
        settings.get_queue_key_for_trial("claude-code", "claude-sonnet-4-6") == expected
    )
    assert settings.get_provider_for_trial("claude-code", None) == "bedrock"


def test_anthropic_hdo_prefix_stays_off_bedrock_queue(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    expected = "anthropic-hdo/claude-sonnet-4-6"

    assert (
        settings.normalize_trial_model("claude-code", "anthropic-hdo/claude-sonnet-4-6")
        == expected
    )
    assert (
        settings.get_provider_for_trial(
            "claude-code", "anthropic-hdo/claude-sonnet-4-6"
        )
        == "anthropic-hdo"
    )
    assert (
        settings.get_queue_key_for_trial(
            "claude-code", "anthropic-hdo/claude-sonnet-4-6"
        )
        == expected
    )
    # Bare Claude ids still take the Bedrock path — HDO is prefix-opt-in only.
    assert (
        settings.normalize_trial_model("claude-code", "claude-sonnet-4-6")
        == "global.anthropic.claude-sonnet-4-6"
    )


def test_anthropic_prefix_stays_off_bedrock_queue(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    expected = "anthropic/claude-sonnet-4-6"

    assert (
        settings.normalize_trial_model("claude-code", "anthropic/claude-sonnet-4-6")
        == expected
    )
    assert (
        settings.get_provider_for_trial("claude-code", "anthropic/claude-sonnet-4-6")
        == "anthropic"
    )
    assert (
        settings.get_queue_key_for_trial("claude-code", "anthropic/claude-sonnet-4-6")
        == expected
    )
    # Bare Claude ids still take the Bedrock path — the direct Anthropic API
    # is prefix-opt-in only, exactly like anthropic-hdo/.
    assert (
        settings.normalize_trial_model("claude-code", "claude-sonnet-4-6")
        == "global.anthropic.claude-sonnet-4-6"
    )


def test_explicit_openai_family_prefixes_win_over_global_default(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-key")
    monkeypatch.setenv(
        "AZURE_OPENAI_ENDPOINT",
        "https://example.openai.azure.com/openai/v1",
    )
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
    monkeypatch.setenv(
        "ODDISH_AZURE_OPENAI_DEPLOYMENTS",
        '{"openai/gpt-5.2":"oddish-gpt"}',
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-public")
    settings = _settings(monkeypatch, clear_openai_env=False)

    # Azure default: bare ids route to Azure, openai/ ids to the platform.
    assert settings.get_openai_route_for_model("gpt-5.2") == "azure"
    assert settings.get_openai_agent_env(model="openai/gpt-5.2") == {
        "OPENAI_API_KEY": "sk-public"
    }
    assert (
        settings.get_openai_agent_env(model="azure/gpt-5.2")["AZURE_OPENAI_DEPLOYMENT"]
        == "oddish-gpt"
    )

    # Flipping the global default moves bare ids only; azure/ stays Azure.
    monkeypatch.setenv("ODDISH_OPENAI_PROVIDER", "openai")
    flipped = _settings(monkeypatch, clear_openai_env=False)
    assert flipped.get_openai_route_for_model("gpt-5.2") == "openai"
    assert flipped.get_openai_route_for_model("azure/gpt-5.2") == "azure"
    assert (
        flipped.get_openai_agent_env(model="azure/gpt-5.2")["AZURE_OPENAI_DEPLOYMENT"]
        == "oddish-gpt"
    )


def test_opus_4_8_maps_to_global_inference_profile(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    # Opus 4.8's invokable Bedrock id is the "global." cross-region inference
    # profile (the bare "anthropic.claude-opus-4-8" foundation-model id is not
    # invokable on-demand via the legacy InvokeModel API Claude Code uses).
    expected = "global.anthropic.claude-opus-4-8"

    assert settings.normalize_trial_model("claude-code", "claude-opus-4-8") == expected
    assert (
        settings.normalize_trial_model("claude-code", "bedrock/claude-opus-4-8")
        == expected
    )
    assert settings.normalize_queue_key("claude-opus-4-8") == expected
    assert settings.normalize_queue_key("claude/claude-opus-4-8") == expected


def test_dotted_marketing_spelling_maps_to_global_inference_profile(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    # The marketing spelling uses a dotted minor version ("claude-opus-4.8"),
    # but the Bedrock table is keyed by the canonical dashed id
    # ("claude-opus-4-8"). The dotted alias must resolve rather than raising
    # (an unmapped Claude id surfaces as a 500 at trial submit).
    assert (
        settings.normalize_trial_model("claude-code", "claude-opus-4.8")
        == "global.anthropic.claude-opus-4-8"
    )
    assert (
        settings.normalize_trial_model("claude-code", "bedrock/claude-opus-4.8")
        == "global.anthropic.claude-opus-4-8"
    )
    # The direct-API route applies the same dotted-alias tolerance: the
    # canonical dashed id is what reaches the Anthropic API.
    assert (
        settings.normalize_trial_model("claude-code", "anthropic/claude-opus-4.8")
        == "anthropic/claude-opus-4-8"
    )
    assert (
        settings.normalize_trial_model("claude-code", "claude-sonnet-4.6")
        == "global.anthropic.claude-sonnet-4-6"
    )
    # Analyzer blocks take their wire id from to_anthropic_api_model_id instead
    # of the trial normalizer, so the alias has to resolve on that path too --
    # otherwise the same id a trial accepts 404s on the analyzer's API call.
    assert to_anthropic_api_model_id("anthropic/claude-opus-4.8") == "claude-opus-4-8"
    assert to_anthropic_api_model_id("claude/claude-sonnet-4.6") == "claude-sonnet-4-6"
    # Only a Claude id behind an Anthropic prefix collapses its dots: OpenAI
    # slugs and Bedrock ids are legitimately dotted.
    assert to_anthropic_api_model_id("openai/gpt-5.3-codex") == "openai/gpt-5.3-codex"
    assert (
        to_anthropic_api_model_id("anthropic.claude-haiku-4-5-20251001-v1:0")
        == "anthropic.claude-haiku-4-5-20251001-v1:0"
    )


def test_fable_5_maps_to_global_inference_profile(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    # Fable 5's invokable Bedrock id is the "global." cross-region inference
    # profile, dateless and without a version suffix (same shape as Opus 4.8).
    # Note the id is "claude-fable-5", NOT "claude-fable-v5" — Bedrock rejects
    # the latter with "The provided model identifier is invalid".
    expected = "global.anthropic.claude-fable-5"

    assert settings.normalize_trial_model("claude-code", "claude-fable-5") == expected
    assert (
        settings.normalize_trial_model("claude-code", "bedrock/claude-fable-5")
        == expected
    )
    assert settings.normalize_queue_key("claude-fable-5") == expected
    assert settings.normalize_queue_key(f"bedrock/{expected}") == expected


def test_sonnet_5_maps_to_global_inference_profile(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    # Sonnet 5's invokable Bedrock id follows the dateless "global." shape of
    # the other 4.6+-generation models (same registration as Opus 5).
    expected = "global.anthropic.claude-sonnet-5"

    assert settings.normalize_trial_model("claude-code", "claude-sonnet-5") == expected
    assert (
        settings.normalize_trial_model("claude-code", "bedrock/claude-sonnet-5")
        == expected
    )
    # The direct-API opt-in needs no Bedrock table entry.
    assert (
        settings.normalize_trial_model("claude-code", "anthropic/claude-sonnet-5")
        == "anthropic/claude-sonnet-5"
    )


def test_bedrock_queue_key_normalization_collapses_aliases(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    expected = "global.anthropic.claude-sonnet-4-6"

    assert settings.normalize_queue_key("claude-sonnet-4-6") == expected
    assert settings.normalize_queue_key("claude/claude-sonnet-4-6") == expected
    assert settings.normalize_queue_key(f"bedrock/{expected}") == expected
    # ``anthropic/`` names the direct Anthropic API and keeps its own bucket.
    assert (
        settings.normalize_queue_key("anthropic/claude-sonnet-4-6")
        == "anthropic/claude-sonnet-4-6"
    )


def test_openrouter_claude_model_routes_through_openrouter(monkeypatch):
    settings = _settings(monkeypatch)

    # An explicit openrouter/ prefix must pin the trial to OpenRouter instead
    # of being rewritten to a Bedrock inference-profile id.
    model = "openrouter/anthropic/claude-opus-4.8"

    assert settings.normalize_trial_model("claude-code", model) == model
    assert settings.get_provider_for_trial("claude-code", model) == "openrouter"
    assert settings.get_queue_key_for_trial("claude-code", model) == model
    assert settings.normalize_queue_key(model) == model


def test_glm_model_routes_to_zai_not_bedrock(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    # GLM runs on the claude-code harness but must NOT inherit claude-code's
    # fixed Bedrock provider/queue -- it gets its own z.ai bucket so it does not
    # contend with heavy Bedrock traffic for concurrency slots.
    for raw in (
        "glm-x-preview[1m]",
        "zai/glm-x-preview[1m]",
        "z-ai/glm-x-preview[1m]",
        "GLM-X-Preview[1M]",
    ):
        assert (
            settings.normalize_trial_model("claude-code", raw)
            == "zai/glm-x-preview[1m]"
        ), raw
        assert settings.get_provider_for_trial("claude-code", raw) == "zai", raw
        assert (
            settings.get_queue_key_for_trial("claude-code", raw)
            == "zai/glm-x-preview[1m]"
        ), raw


def test_minimax_model_routes_to_minimax_not_bedrock(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    # MiniMax runs on the claude-code harness but must get its own provider /
    # queue bucket (the canonical id is lowercased for storage/queueing).
    for raw in ("MiniMax-M3", "minimax/MiniMax-M3", "minimax-m3"):
        assert (
            settings.normalize_trial_model("claude-code", raw) == "minimax/minimax-m3"
        ), raw
        assert settings.get_provider_for_trial("claude-code", raw) == "minimax", raw
        assert (
            settings.get_queue_key_for_trial("claude-code", raw) == "minimax/minimax-m3"
        ), raw


def test_moonshot_model_routes_to_moonshot_not_bedrock(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    for raw in (
        "kimi-k2.7-code",
        "moonshot/kimi-k2.7-code",
        "kimi/kimi-k2.7-code",
        "moonshotai/kimi-k2.7-code",
    ):
        assert (
            settings.normalize_trial_model("claude-code", raw)
            == "moonshot/kimi-k2.7-code"
        ), raw
        assert settings.get_provider_for_trial("claude-code", raw) == "moonshot", raw
        assert (
            settings.get_queue_key_for_trial("claude-code", raw)
            == "moonshot/kimi-k2.7-code"
        ), raw


def test_fireworks_models_route_to_fireworks_not_direct_providers(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    # The consolidation route: an explicit ``fireworks/`` prefix sends GLM /
    # MiniMax / Kimi to one shared Fireworks bucket instead of each model's own
    # direct provider. Friendly spellings collapse to the canonical short id so
    # every spelling shares one queue/provider bucket.
    cases = {
        "fireworks/glm-5.2": "fireworks/glm-5p2",
        "fw/glm-5p2": "fireworks/glm-5p2",
        "fireworks/minimax-m3": "fireworks/minimax-m3",
        "fireworks/kimi-k2.7": "fireworks/kimi-k2p7-code",
        "fireworks/kimi-k2.7-code": "fireworks/kimi-k2p7-code",
        "fireworks/kimi-k2p7-code": "fireworks/kimi-k2p7-code",
    }
    for raw, canonical in cases.items():
        assert settings.normalize_trial_model("claude-code", raw) == canonical, raw
        assert settings.get_provider_for_trial("claude-code", raw) == "fireworks", raw
        assert settings.get_queue_key_for_trial("claude-code", raw) == canonical, raw


def test_grok_build_xai_model_routes_to_xai(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)
    model = "xai/redacted-model"

    assert normalize_model_id(" XAI / redacted-model ") == model
    assert settings.normalize_trial_model("grok-build", model) == model
    assert settings.get_provider_for_trial("grok-build", model) == "xai"
    assert settings.get_queue_key_for_trial("grok-build", model) == model
    assert settings.normalize_queue_key(model) == model


def test_meta_model_routes_to_meta_for_mini_swe_agent(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)
    model = "meta/llama-eval-model"

    assert normalize_model_id(" Meta / Llama Eval Model ") == model
    assert settings.normalize_trial_model("mini-swe-agent", model) == model
    assert settings.get_provider_for_trial("mini-swe-agent", model) == "meta"
    assert settings.get_queue_key_for_trial("mini-swe-agent", model) == model
    assert settings.normalize_queue_key(model) == model


def test_meta_agent_env_includes_configured_session_controls(monkeypatch):
    monkeypatch.setenv("ODDISH_META_EVAL_NAME", "SWE Marathon")
    monkeypatch.setenv("ODDISH_META_SESSION_ID", "swe-marathon--123456")
    settings = _settings(monkeypatch, clear_openai_env=False)

    env = settings.get_meta_agent_env()

    assert env["ODDISH_META_EVAL_NAME"] == "SWE Marathon"
    assert env["ODDISH_META_SESSION_ID"] == "swe-marathon--123456"
    assert env["MSWEA_API_KEY"] == "${META_API_KEY}"
    # LiteLLM's openai/ provider authenticates from OPENAI_API_KEY.
    assert env["OPENAI_API_KEY"] == "${META_API_KEY}"
    assert "OPENAI_API_BASE" not in env


def test_grok_provider_prefix_canonicalizes_to_xai(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    assert (
        settings.normalize_trial_model("grok-build", "grok/redacted-model")
        == "xai/redacted-model"
    )
    assert (
        settings.get_queue_key_for_trial("grok-build", "grok/redacted-model")
        == "xai/redacted-model"
    )
    assert settings.get_provider_for_trial("grok-build", "grok/redacted-model") == "xai"


def test_grok_build_without_model_uses_xai_provider_bucket(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    assert settings.get_provider_for_trial("grok-build", None) == "xai"
    assert settings.get_queue_key_for_trial("grok-build", None) == "xai"


def test_bare_glm_minimax_kimi_keep_direct_provider_routes(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    # Without the ``fireworks/`` prefix the existing per-vendor direct routes are
    # unchanged -- adding Fireworks must not hijack them.
    assert settings.get_provider_for_trial("claude-code", "glm-x-preview[1m]") == "zai"
    assert settings.get_provider_for_trial("claude-code", "minimax-m3") == "minimax"
    assert (
        settings.get_provider_for_trial("claude-code", "kimi-k2.7-code") == "moonshot"
    )


def test_fireworks_queue_keys_have_independent_concurrency(monkeypatch):
    monkeypatch.setenv(
        "ODDISH_MODEL_CONCURRENCY_OVERRIDES",
        '{"fireworks/glm-5p2": 4, "fireworks/kimi-k2p7-code": 6}',
    )
    settings = Settings(_env_file=None)

    assert settings.get_model_concurrency("fireworks/glm-5p2") == 4
    assert settings.get_model_concurrency("fireworks/kimi-k2p7-code") == 6


def test_openrouter_kimi_model_is_not_hijacked_to_moonshot(monkeypatch):
    settings = _settings(monkeypatch)

    # An explicit openrouter/ prefix keeps OpenRouter routing -- the direct
    # Moonshot path must not steal it (both columns run concurrently).
    model = "openrouter/moonshotai/kimi-k2.7-code"

    assert settings.normalize_trial_model("claude-code", model) == model
    assert settings.get_provider_for_trial("claude-code", model) == "openrouter"
    assert settings.get_queue_key_for_trial("claude-code", model) == model


def test_minimax_moonshot_queue_keys_have_independent_concurrency(monkeypatch):
    monkeypatch.setenv(
        "ODDISH_MODEL_CONCURRENCY_OVERRIDES",
        '{"minimax/minimax-m3": 4, "moonshot/kimi-k2.7-code": 6}',
    )
    settings = Settings(_env_file=None)

    assert settings.get_model_concurrency("minimax/minimax-m3") == 4
    assert settings.get_model_concurrency("moonshot/kimi-k2.7-code") == 6


def test_glm_queue_key_has_independent_concurrency(monkeypatch):
    monkeypatch.setenv(
        "ODDISH_MODEL_CONCURRENCY_OVERRIDES",
        '{"zai/glm-x-preview[1m]": 4, "global.anthropic.claude-opus-4-8": 64}',
    )
    settings = Settings(_env_file=None)

    # The GLM bucket is keyed separately from any Bedrock model, so capping GLM
    # concurrency does not throttle (and is not throttled by) Bedrock trials.
    assert settings.get_model_concurrency("zai/glm-x-preview[1m]") == 4
    assert settings.get_model_concurrency("global.anthropic.claude-opus-4-8") == 64


def test_legacy_unmapped_claude_queue_key_does_not_break_reads(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)
    legacy_key = "anthropic/claude-sonnet-4-6-20250514"

    assert settings.normalize_queue_key(legacy_key) == legacy_key
    # ``anthropic/`` now names the direct Anthropic API, where dated ids are
    # valid — the id no longer needs a Bedrock mapping to execute.
    assert settings.normalize_trial_model("claude-code", legacy_key) == legacy_key
    # A bare unmapped Claude id still fails loudly on the Bedrock chokepoint.
    with pytest.raises(ValueError):
        settings.normalize_trial_model("claude-code", "claude-sonnet-4-6-20250514")


def test_openai_provider_defaults_to_azure_for_bare_ids(monkeypatch):
    settings = _settings(monkeypatch)

    assert settings.get_openai_provider() == "azure"
    assert settings.get_openai_route_for_model("gpt-5.2") == "azure"
    with pytest.raises(RuntimeError, match="Azure OpenAI is the default"):
        settings.get_openai_runtime_env(model="gpt-5.2")
    # An explicit ``openai/`` id names the public platform; without a key it
    # fails loudly on the platform requirement, never silently onto Azure.
    assert settings.get_openai_route_for_model("openai/gpt-5.2") == "openai"
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is required"):
        settings.get_openai_runtime_env(model="openai/gpt-5.2")


def test_openai_api_key_alone_does_not_reroute_bare_ids(monkeypatch):
    settings = _settings(monkeypatch, OPENAI_API_KEY="sk-test")

    assert settings.get_openai_provider() == "azure"
    # A configured platform key must not flip bare-id routing off Azure...
    with pytest.raises(RuntimeError, match="Azure OpenAI is the default"):
        settings.get_openai_runtime_env(model="gpt-5.2")
    # ...but an explicit ``openai/`` id runs on the public platform with it.
    assert settings.get_openai_runtime_env(model="openai/gpt-5.2") == {
        "OPENAI_API_KEY": "sk-test"
    }
    assert settings.get_openai_agent_env(model="openai/gpt-5.2") == {
        "OPENAI_API_KEY": "sk-test"
    }


def test_azure_openai_deployment_mapping_accepts_prefixed_and_bare_models(
    monkeypatch,
):
    settings = _settings(
        monkeypatch,
        azure_openai_deployments={
            "openai/gpt-5.2": "azure-gpt-5-2",
            "gpt-5.4": "azure-gpt-5-4",
        },
    )

    assert settings.resolve_azure_openai_deployment("openai/gpt-5.2") == (
        "azure-gpt-5-2"
    )
    assert settings.resolve_azure_openai_deployment("gpt-5.2") == "azure-gpt-5-2"
    assert settings.resolve_azure_openai_deployment("openai/gpt-5.4") == (
        "azure-gpt-5-4"
    )


def test_azure_openai_deployment_mapping_normalizes_model_keys(monkeypatch):
    monkeypatch.setenv(
        "ODDISH_AZURE_OPENAI_DEPLOYMENTS",
        '{" OpenAI / GPT 5.2 ":"azure-gpt-5-2"}',
    )
    settings = _settings(monkeypatch, clear_openai_env=False)

    assert settings.azure_openai_deployments == {"openai/gpt-5.2": "azure-gpt-5-2"}
    assert settings.resolve_azure_openai_deployment("openai/gpt-5.2") == (
        "azure-gpt-5-2"
    )


def test_missing_azure_openai_deployment_mapping_fails_loudly(monkeypatch):
    settings = _settings(
        monkeypatch,
        azure_openai_deployments={"openai/gpt-5.2": "azure-gpt-5-2"},
    )

    with pytest.raises(ValueError, match="No Azure OpenAI deployment mapping"):
        settings.resolve_azure_openai_deployment("openai/gpt-5.4")


def test_openai_queue_key_preserves_requested_model_name(monkeypatch):
    settings = _settings(
        monkeypatch,
        azure_openai_deployments={"openai/gpt-5.2": "azure-gpt-5-2"},
    )

    assert settings.normalize_trial_model("codex", "openai/gpt-5.2") == (
        "openai/gpt-5.2"
    )
    assert settings.get_queue_key_for_trial("codex", "openai/gpt-5.2") == (
        "openai/gpt-5.2"
    )


def test_azure_openai_runtime_env_excludes_public_openai_key(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-key")
    monkeypatch.setenv(
        "AZURE_OPENAI_ENDPOINT",
        "https://example.openai.azure.com/openai/v1",
    )
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
    monkeypatch.setenv(
        "ODDISH_AZURE_OPENAI_DEPLOYMENTS",
        '{"openai/gpt-5.2":"oddish-gpt"}',
    )
    settings = _settings(monkeypatch, clear_openai_env=False)

    # An explicit ``azure/`` id names the Azure route; the deployment map
    # stays keyed by the conventional ``openai/`` form.
    env = settings.get_openai_runtime_env(model="azure/gpt-5.2")

    assert env["AZURE_OPENAI_API_KEY"] == "az-key"
    assert env["AZURE_OPENAI_ENDPOINT"] == "https://example.openai.azure.com/openai/v1"
    assert env["AZURE_OPENAI_API_VERSION"] == "2025-01-01-preview"
    assert env["AZURE_OPENAI_DEPLOYMENT"] == "oddish-gpt"
    assert env["OPENAI_API_VERSION"] == "2025-01-01-preview"
    assert "OPENAI_API_KEY" not in env


def test_azure_openai_agent_env_uses_compatible_base_url(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-key")
    monkeypatch.setenv(
        "AZURE_OPENAI_ENDPOINT",
        "https://example.openai.azure.com/openai/v1",
    )
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
    monkeypatch.setenv(
        "ODDISH_AZURE_OPENAI_DEPLOYMENTS",
        '{"openai/gpt-5.2":"oddish-gpt"}',
    )
    settings = _settings(monkeypatch, clear_openai_env=False)

    env = settings.get_openai_agent_env(model="azure/gpt-5.2")

    assert env["OPENAI_API_KEY"] == "az-key"
    assert env["OPENAI_BASE_URL"] == "https://example.openai.azure.com/openai/v1"
    assert env["AZURE_API_KEY"] == "az-key"
    assert env["AZURE_API_BASE"] == "https://example.openai.azure.com/openai/v1"
    assert env["AZURE_API_VERSION"] == "2025-01-01-preview"


def test_foundry_openai_v1_endpoint_is_allowed(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-key")
    monkeypatch.setenv(
        "AZURE_OPENAI_ENDPOINT",
        "https://example.services.ai.azure.com/openai/v1",
    )
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
    monkeypatch.setenv(
        "ODDISH_AZURE_OPENAI_DEPLOYMENTS",
        '{"openai/gpt-5.2":"gpt-5.2"}',
    )
    settings = _settings(monkeypatch, clear_openai_env=False)

    assert (
        settings.get_azure_openai_base_url()
        == "https://example.services.ai.azure.com/openai/v1"
    )


def test_azure_openai_agent_env_rejects_foundry_project_endpoint(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-key")
    monkeypatch.setenv(
        "AZURE_OPENAI_ENDPOINT",
        "https://example.services.ai.azure.com/api/projects/oddish",
    )
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
    monkeypatch.setenv(
        "ODDISH_AZURE_OPENAI_DEPLOYMENTS",
        '{"openai/gpt-5.2":"oddish-gpt"}',
    )
    settings = _settings(monkeypatch, clear_openai_env=False)

    with pytest.raises(RuntimeError, match="Do not use the Foundry project endpoint"):
        settings.get_openai_agent_env(model="azure/gpt-5.2")


def test_public_openai_requires_explicit_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    settings = _settings(
        monkeypatch,
        clear_openai_env=False,
        openai_provider="openai",
    )

    assert settings.get_openai_provider() == "openai"
    assert settings.get_openai_runtime_env() == {"OPENAI_API_KEY": "sk-test"}


def test_bare_openai_ids_queue_under_their_resolved_transport(monkeypatch):
    settings = _settings(monkeypatch, clear_openai_env=False)

    # Azure default: a bare id runs on Azure, so it shares the azure/<slug>
    # bucket with explicit azure/ ids (same deployment quota) — never the
    # public-platform openai/<slug> bucket.
    monkeypatch.setattr(settings, "openai_provider", "azure")
    assert settings.normalize_queue_key("gpt-5.4") == "azure/gpt-5.4"
    assert settings.normalize_queue_key("azure/gpt-5.4") == "azure/gpt-5.4"
    # azure_openai/ names the same transport — one bucket, one spelling.
    assert settings.normalize_queue_key("azure_openai/gpt-5.4") == "azure/gpt-5.4"
    assert settings.normalize_queue_key("openai/gpt-5.4") == "openai/gpt-5.4"

    # Public default: bare ids genuinely run on the platform bucket.
    monkeypatch.setattr(settings, "openai_provider", "openai")
    assert settings.normalize_queue_key("gpt-5.4") == "openai/gpt-5.4"
