"""Google Vertex AI provider: one agent-agnostic profile for every trial."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from harbor.models.trial.config import AgentConfig
from harbor.trial.hooks import TrialEvent
from pydantic import SecretStr

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import (  # noqa: E402
    VertexAiConfigError,
    is_vertex_ai_model,
    settings,
    to_bedrock_model_id,
    to_vertex_ai_model_id,
)
from oddish.workers.harbor import vertex_ai  # noqa: E402
from oddish.workers.harbor.agent_config import _build_agent_config  # noqa: E402

_SA_JSON = json.dumps(
    {
        "type": "service_account",
        "project_id": "oddish-vertex",
        "private_key_id": "abc123",
        "private_key": "-----BEGIN PRIVATE KEY-----\nSECRETMATERIAL\n-----END PRIVATE KEY-----\n",
        "client_email": "trials@oddish-vertex.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
)


@pytest.fixture
def service_account(monkeypatch):
    monkeypatch.setattr(settings, "vertex_ai_project_id", "oddish-vertex")
    monkeypatch.setattr(settings, "vertex_ai_location", "global")
    monkeypatch.setattr(settings, "vertex_ai_credentials_json", SecretStr(_SA_JSON))
    monkeypatch.setattr(settings, "vertex_ai_api_key", None)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", raising=False)
    monkeypatch.setattr(vertex_ai, "_WORKER_CREDENTIAL_FILES", {})
    return settings.vertex_ai_config()


@pytest.fixture
def express(monkeypatch):
    monkeypatch.setattr(settings, "vertex_ai_project_id", None)
    monkeypatch.setattr(settings, "vertex_ai_location", "us-east5")
    monkeypatch.setattr(settings, "vertex_ai_credentials_json", None)
    monkeypatch.setattr(settings, "vertex_ai_api_key", SecretStr("express-key"))
    return settings.vertex_ai_config()


def _build(agent: str, model: str, env: dict | None = None) -> AgentConfig:
    raw = {"agent_config": {"name": agent, "model_name": model, "env": env or {}}}
    return _build_agent_config(agent=agent, model=model, raw_harbor_config=raw)


# --- canonical id, provider, queue --------------------------------------------


@pytest.mark.parametrize(
    "submitted",
    [
        "vertex_ai/gemini-3.8-flash",
        "vertex/gemini-3.8-flash",
        "vertex-ai/gemini-3.8-flash",
        "google-vertex/gemini-3.8-flash",
        "Vertex_AI/gemini-3.8-flash",
    ],
)
def test_vertex_prefixes_canonicalize_to_one_id_and_queue_key(submitted):
    for agent in ("gemini-cli", "claude-code", "mini-swe-agent", "opencode"):
        assert (
            settings.normalize_trial_model(agent, submitted)
            == "vertex_ai/gemini-3.8-flash"
        )
        assert (
            settings.get_queue_key_for_trial(agent, submitted)
            == "vertex_ai/gemini-3.8-flash"
        )
        assert settings.get_provider_for_trial(agent, submitted) == "vertex_ai"
    # Admin overrides and the endpoint check call this on raw input.
    assert settings.normalize_queue_key(submitted) == "vertex_ai/gemini-3.8-flash"


def test_vertex_claude_ids_stay_off_bedrock_and_pass_through():
    for bare in (
        "claude-sonnet-5",
        "claude-sonnet-4-5@20250929",
        "claude-haiku-4-5@20251001",
    ):
        model = f"vertex/{bare}"
        assert (
            settings.normalize_trial_model("claude-code", model) == f"vertex_ai/{bare}"
        )
        assert settings.get_provider_for_trial("claude-code", model) == "vertex_ai"
        assert to_bedrock_model_id(f"vertex_ai/{bare}") == f"vertex_ai/{bare}"
    assert not is_vertex_ai_model("claude-sonnet-5")
    assert not is_vertex_ai_model("gemini-3.8-flash")
    assert settings.normalize_queue_key("vertex_ai") == "default"


def test_alias_and_unknown_spellings_disagree_on_purpose():
    assert to_vertex_ai_model_id("google-vertex/x") == "vertex_ai/x"
    assert to_vertex_ai_model_id("vertexai/x") == "vertexai/x"
    assert (
        settings.get_provider_for_trial("mini-swe-agent", "vertexai/x") != "vertex_ai"
    )


def test_ai_studio_route_is_untouched_and_distinct(service_account, monkeypatch):
    """The Gemini API key route (Google AI Studio) and Vertex are two providers.

    The same bare model id runs on either; the prefix decides the provider,
    queue key, credential, and host, and a Vertex-configured worker changes
    nothing for an AI Studio trial.
    """
    from oddish.workers.harbor import model_hosts
    from oddish.workers.queue.job_tokens import scoped_model_env

    monkeypatch.setattr(settings, "gemini_api_key", "studio-key")
    for studio in ("gemini/gemini-3.8-flash", "google/gemini-3.8-flash"):
        assert settings.get_provider_for_trial("gemini-cli", studio) == "gemini"
        assert settings.get_queue_key_for_trial("gemini-cli", studio) == studio
        built = _build("gemini-cli", studio)
        assert built.model_name == studio
        for key in (
            "ODDISH_VERTEX_AI_MODE",
            "CLAUDE_CODE_USE_VERTEX",
            "GOOGLE_GENAI_USE_VERTEXAI",
            "GEMINI_API_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
        ):
            assert key not in built.env
        assert model_hosts.outbound_hosts_for_model(studio, agent_env=built.env) == [
            "generativelanguage.googleapis.com"
        ]
    assert scoped_model_env(
        agent="gemini-cli", model="gemini/gemini-3.8-flash", settings=settings
    ) == {
        "GEMINI_API_KEY": "studio-key",
        "GOOGLE_GENERATIVE_AI_API_KEY": "studio-key",
    }
    assert vertex_ai.plan_for_model("gemini/gemini-3.8-flash") is None
    # One model, two routes, two buckets.
    assert (
        settings.get_queue_key_for_trial("gemini-cli", "vertex_ai/gemini-3.8-flash")
        == "vertex_ai/gemini-3.8-flash"
    )
    assert (
        settings.get_queue_key_for_trial("gemini-cli", "gemini/gemini-3.8-flash")
        == "gemini/gemini-3.8-flash"
    )


# --- configuration --------------------------------------------------------------


def test_vertex_ai_config_modes_and_errors(monkeypatch):
    monkeypatch.setattr(settings, "vertex_ai_credentials_json", None)
    monkeypatch.setattr(settings, "vertex_ai_api_key", None)
    monkeypatch.setattr(settings, "vertex_ai_project_id", None)
    with pytest.raises(VertexAiConfigError):
        settings.vertex_ai_config()

    monkeypatch.setattr(settings, "vertex_ai_credentials_json", SecretStr(_SA_JSON))
    with pytest.raises(VertexAiConfigError):
        settings.vertex_ai_config()  # a key without a project

    monkeypatch.setattr(settings, "vertex_ai_project_id", "p")
    monkeypatch.setattr(settings, "vertex_ai_location", " US-East5 ")
    config = settings.vertex_ai_config()
    assert config.mode == "service_account"
    assert config.location == "us-east5"
    assert config.credentials_json == _SA_JSON

    monkeypatch.setattr(settings, "vertex_ai_credentials_json", None)
    monkeypatch.setattr(settings, "vertex_ai_api_key", SecretStr("k"))
    config = settings.vertex_ai_config()
    assert config.mode == "api_key"
    assert config.location == "global"  # express mode is global-only
    assert config.api_key == "k"


# --- the profile ------------------------------------------------------------------


def _profile_keys(env: dict[str, str]) -> dict[str, str]:
    return {
        k: v
        for k, v in env.items()
        if k not in vertex_ai._CLAUDE_MODEL_ALIAS_KEYS and k != "ANTHROPIC_MODEL"
    }


def test_profile_is_identical_across_agents_and_keeps_the_canonical_id(service_account):
    profile = vertex_ai.vertex_ai_agent_env(
        service_account, "vertex_ai/gemini-3.8-flash"
    )
    envs = {}
    for agent in ("claude-code", "gemini-cli", "mini-swe-agent", "codex"):
        built = _build(agent, "vertex_ai/gemini-3.8-flash")
        assert built.model_name == "vertex_ai/gemini-3.8-flash"
        envs[agent] = dict(built.env)
    # Harnesses that add nothing of their own receive exactly the profile;
    # every harness receives at least the profile, unchanged.
    assert envs["claude-code"] == envs["gemini-cli"] == envs["codex"] == profile
    for agent, built_env in envs.items():
        assert {k: built_env[k] for k in profile} == profile, agent
    env = envs["gemini-cli"]
    assert env["GOOGLE_GENAI_USE_VERTEXAI"] == "true"
    assert env["GOOGLE_GENAI_USE_ENTERPRISE"] == "true"
    assert env["GOOGLE_CLOUD_PROJECT"] == env["VERTEXAI_PROJECT"] == "oddish-vertex"
    assert (
        env["GOOGLE_VERTEX_PROJECT"]
        == env["ANTHROPIC_VERTEX_PROJECT_ID"]
        == "oddish-vertex"
    )
    assert env["GOOGLE_CLOUD_LOCATION"] == env["VERTEXAI_LOCATION"] == "global"
    assert env["GOOGLE_VERTEX_LOCATION"] == env["VERTEX_LOCATION"] == "global"
    assert env["CLAUDE_CODE_USE_VERTEX"] == "1"
    assert env["CLOUD_ML_REGION"] == "global"
    assert env["ODDISH_VERTEX_AI_MODE"] == "service_account"
    assert (
        env["GOOGLE_APPLICATION_CREDENTIALS"]
        == "/tmp/oddish-vertex/service-account.json"
    )
    assert env["VERTEXAI_CREDENTIALS"] == "/tmp/oddish-vertex/service-account.json"
    # Competing selectors are neutralized; the boolean is a real boolean.
    assert (
        env["CLAUDE_CODE_USE_BEDROCK"] == "" and env["AWS_BEARER_TOKEN_BEDROCK"] == ""
    )
    assert env["GEMINI_API_KEY"] == "" and env["GOOGLE_API_KEY"] == ""
    assert env["GOOGLE_GENERATIVE_AI_API_KEY"] == ""
    assert env["GEMINI_FORCE_OAUTH"] == "false" and env["GEMINI_OAUTH_CREDS_PATH"] == ""
    assert env["ANTHROPIC_BASE_URL"] == ""
    # The secret never enters the agent env.
    assert not any("SECRETMATERIAL" in v for v in env.values())
    assert "ANTHROPIC_MODEL" not in env


def test_gemini_force_oauth_value_parses_in_harbors_resolver(service_account):
    from harbor.utils.env import parse_bool_env_value

    env = vertex_ai.vertex_ai_agent_env(service_account, "vertex_ai/gemini-3.8-flash")
    assert (
        parse_bool_env_value(
            env["GEMINI_FORCE_OAUTH"], name="GEMINI_FORCE_OAUTH", default=False
        )
        is False
    )
    with pytest.raises(ValueError):
        parse_bool_env_value("", name="GEMINI_FORCE_OAUTH", default=False)


def test_claude_model_variables_only_for_claude_ids(service_account):
    gemini = _build("claude-code", "vertex_ai/gemini-3.8-flash")
    assert "ANTHROPIC_MODEL" not in gemini.env
    claude = _build(
        "claude-code",
        "vertex/claude-sonnet-4-5@20250929",
        env={"CLAUDE_CODE_SUBAGENT_MODEL": "claude-haiku-4-5@20251001"},
    )
    assert claude.model_name == "vertex_ai/claude-sonnet-4-5@20250929"
    assert claude.env["ANTHROPIC_MODEL"] == "claude-sonnet-4-5@20250929"
    for key in (
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_FABLE_MODEL",
    ):
        assert claude.env[key] == "claude-sonnet-4-5@20250929"
    # Alias pins are defaults; a submitted value survives.
    assert claude.env["CLAUDE_CODE_SUBAGENT_MODEL"] == "claude-haiku-4-5@20251001"


def test_submitted_competing_credentials_are_overwritten(service_account):
    built = _build(
        "gemini-cli",
        "vertex_ai/gemini-3.8-flash",
        env={
            "GEMINI_API_KEY": "leaked",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "ANTHROPIC_BASE_URL": "https://relay.example",
            "GOOGLE_CLOUD_PROJECT": "someone-elses-project",
        },
    )
    assert built.env["GEMINI_API_KEY"] == ""
    assert built.env["CLAUDE_CODE_USE_BEDROCK"] == ""
    assert built.env["ANTHROPIC_BASE_URL"] == ""
    assert built.env["GOOGLE_CLOUD_PROJECT"] == "oddish-vertex"


def test_express_mode_profile_uses_a_template_and_no_file(express):
    env = vertex_ai.vertex_ai_agent_env(express, "vertex_ai/gemini-3.8-flash")
    assert env["GOOGLE_API_KEY"] == "${VERTEX_AI_API_KEY}"
    assert env["VERTEXAI_CREDENTIALS"] == ""
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in env
    assert env["ODDISH_VERTEX_AI_MODE"] == "api_key"
    assert env["GOOGLE_CLOUD_LOCATION"] == "global"
    assert "GOOGLE_CLOUD_PROJECT" not in env
    assert vertex_ai.worker_credentials_path(express) is None
    process = vertex_ai.vertex_ai_process_env(express, None)
    assert process["GOOGLE_API_KEY"] == process["VERTEX_AI_API_KEY"] == "express-key"


def test_unconfigured_worker_fails_at_config_build(monkeypatch):
    monkeypatch.setattr(settings, "vertex_ai_credentials_json", None)
    monkeypatch.setattr(settings, "vertex_ai_api_key", None)
    with pytest.raises(VertexAiConfigError, match="VERTEX_AI_CREDENTIALS_JSON"):
        _build("gemini-cli", "vertex_ai/gemini-3.8-flash")


def test_gateway_routed_analysis_trial_skips_the_profile(service_account):
    """A QA-gateway trial supplies its own Anthropic route; the profile stays out."""
    gateway_env = {
        "ODDISH_QA_MODEL_ROUTED": "1",
        "ANTHROPIC_BASE_URL": "https://gateway.example/qa-model",
        "ANTHROPIC_API_KEY": "job.token",
        "ANTHROPIC_MODEL": "claude-sonnet-5",
    }
    built = _build_agent_config(
        agent="claude-code",
        model="vertex_ai/claude-sonnet-5",
        raw_harbor_config={
            "agent_config": {
                "name": "claude-code",
                "model_name": "vertex_ai/claude-sonnet-5",
            }
        },
        probe_oddish_env=gateway_env,
    )
    assert "ODDISH_VERTEX_AI_MODE" not in built.env
    assert "CLAUDE_CODE_USE_VERTEX" not in built.env
    assert built.env["ANTHROPIC_BASE_URL"] == "https://gateway.example/qa-model"
    assert built.model_name == "claude-sonnet-5"


def test_effective_model_comes_from_the_stored_agent_config(
    service_account, tmp_path, monkeypatch
):
    """A trial row with no model still resolves its plan from agent_config.model_name.

    The routed builder installs the profile for that id, so the credential
    plan and the Bedrock blanks must key on the same effective model, not on
    the bare ``model`` argument.
    """
    from harbor.models.environment_type import EnvironmentType
    from harbor.models.trial.config import EnvironmentConfig

    from oddish.workers.harbor import ephemeral

    monkeypatch.setattr(vertex_ai.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        ephemeral, "_supports_auto_restricted_agent_network", lambda **_: False
    )
    raw = {
        "agent_config": {
            "name": "gemini-cli",
            "model_name": "vertex_ai/gemini-3.8-flash",
        }
    }
    built = _build_agent_config(agent="gemini-cli", model=None, raw_harbor_config=raw)
    assert built.env["ODDISH_VERTEX_AI_MODE"] == "service_account"
    overrides = ephemeral._runtime_env_overrides(
        agent="gemini-cli", model=None, raw_harbor_config=raw, is_probe=False
    )
    assert overrides["CLAUDE_CODE_USE_BEDROCK"] == ""
    payload = ephemeral._build_payload(
        task_path=tmp_path / "task",
        jobs_dir=tmp_path / "jobs",
        outcome_path=tmp_path / "outcome.json",
        agent="gemini-cli",
        model=None,
        environment_config=EnvironmentConfig(type=EnvironmentType.MODAL),
        raw_harbor_config=raw,
        is_probe=False,
    )
    assert payload["vertex_ai"] is not None
    assert payload["vertex_ai"]["worker_path"]
    assert payload["agent_config"]["env"]["GOOGLE_APPLICATION_CREDENTIALS"] == (
        "/tmp/oddish-vertex/service-account.json"
    )


def test_kimi_preprocessing_keeps_an_explicit_vertex_id(service_account):
    built = _build("kimi-claude-code", "vertex_ai/claude-sonnet-5")
    assert built.model_name == "vertex_ai/claude-sonnet-5"
    assert built.env["CLAUDE_CODE_USE_VERTEX"] == "1"
    moonshot = _build("kimi-claude-code", "kimi-k2.7-code")
    assert moonshot.model_name == "moonshot/kimi-k2.7-code"


# --- worker side: file, process env, redaction, hook ----------------------------


def test_worker_credentials_file_is_private_and_reused(
    service_account, tmp_path, monkeypatch
):
    monkeypatch.setattr(vertex_ai.tempfile, "gettempdir", lambda: str(tmp_path))
    first = vertex_ai.worker_credentials_path(service_account)
    second = vertex_ai.worker_credentials_path(service_account)
    assert first == second
    assert first.read_text() == _SA_JSON
    assert oct(first.stat().st_mode & 0o777) == "0o600"
    assert oct(first.parent.stat().st_mode & 0o777) == "0o700"


def test_process_env_prefers_vertexai_credentials_and_respects_gke_adc(
    service_account, monkeypatch
):
    path = Path("/private/worker/sa.json")
    env = vertex_ai.vertex_ai_process_env(service_account, path)
    assert env["GOOGLE_APPLICATION_CREDENTIALS"] == str(path)
    assert env["VERTEXAI_CREDENTIALS"] == str(path)
    assert (
        env["CLAUDE_CODE_USE_BEDROCK"] == "" and env["AWS_BEARER_TOKEN_BEDROCK"] == ""
    )
    assert env["VERTEXAI_PROJECT"] == "oddish-vertex"
    # A GKE worker keeps its own service account as the process-level ADC path;
    # LiteLLM host-side harnesses still get the Vertex key.
    monkeypatch.setenv(
        "GOOGLE_APPLICATION_CREDENTIALS_JSON", '{"type": "service_account"}'
    )
    env = vertex_ai.vertex_ai_process_env(service_account, path)
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in env
    assert env["VERTEXAI_CREDENTIALS"] == str(path)


def test_redaction_covers_the_json_and_its_private_key(service_account, express):
    from oddish.workers.harbor.runner import _runtime_transport_redactions

    replacements = _runtime_transport_redactions(
        vertex_ai.redaction_env(service_account)
    )
    assert replacements[_SA_JSON] == "[REDACTED]"
    assert replacements[json.loads(_SA_JSON)["private_key"]] == "[REDACTED]"
    assert (
        _runtime_transport_redactions(vertex_ai.redaction_env(express))["express-key"]
        == "[REDACTED]"
    )


class _FakeEnvironment:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def exec(self, command, user=None, **_):
        self.calls.append(("exec", command, user))

    async def upload_file(self, source_path, target_path):
        self.calls.append(("upload", source_path, target_path))


@pytest.mark.asyncio
async def test_upload_hook_places_a_world_readable_file_on_agent_start():
    hook = vertex_ai.agent_started_upload_hook(Path("/private/worker/sa.json"))
    environment = _FakeEnvironment()
    await hook(
        SimpleNamespace(event=TrialEvent.ENVIRONMENT_START, environment=environment)
    )
    assert environment.calls == []
    await hook(SimpleNamespace(event=TrialEvent.AGENT_START, environment=environment))
    assert environment.calls == [
        ("exec", "mkdir -p /tmp/oddish-vertex && chmod 755 /tmp/oddish-vertex", "root"),
        (
            "upload",
            "/private/worker/sa.json",
            "/tmp/oddish-vertex/service-account.json",
        ),
        ("exec", "chmod 644 /tmp/oddish-vertex/service-account.json", "root"),
    ]
    await hook(SimpleNamespace(event=TrialEvent.AGENT_START, environment=environment))
    assert len(environment.calls) == 6  # idempotent re-run for a later step
    await hook(SimpleNamespace(event=TrialEvent.AGENT_START, environment=None))


def test_plan_for_model_is_none_off_vertex_and_complete_on_it(
    service_account, tmp_path, monkeypatch
):
    monkeypatch.setattr(vertex_ai.tempfile, "gettempdir", lambda: str(tmp_path))
    assert vertex_ai.plan_for_model("gemini/gemini-3.8-flash") is None
    plan = vertex_ai.plan_for_model("vertex_ai/gemini-3.8-flash")
    assert (
        plan is not None
        and plan.worker_path is not None
        and plan.upload_hook is not None
    )
    assert plan.process_env["VERTEXAI_CREDENTIALS"] == str(plan.worker_path)
    assert "VERTEX_AI_CREDENTIALS_JSON" in plan.redaction_env


# --- egress hosts and restricted profiles ---------------------------------------


def test_vertex_hosts_follow_the_location_and_mode():
    assert vertex_ai.vertex_hosts("global") == [
        "aiplatform.googleapis.com",
        "oauth2.googleapis.com",
    ]
    assert vertex_ai.vertex_hosts(None) == [
        "aiplatform.googleapis.com",
        "oauth2.googleapis.com",
    ]
    assert vertex_ai.vertex_hosts("us") == [
        "aiplatform.us.rep.googleapis.com",
        "oauth2.googleapis.com",
    ]
    assert vertex_ai.vertex_hosts("eu") == [
        "aiplatform.eu.rep.googleapis.com",
        "oauth2.googleapis.com",
    ]
    assert vertex_ai.vertex_hosts("us-east5") == [
        "us-east5-aiplatform.googleapis.com",
        "oauth2.googleapis.com",
    ]
    assert vertex_ai.vertex_hosts("us-east5", mode="api_key") == [
        "aiplatform.googleapis.com"
    ]


def test_outbound_hosts_for_vertex_models(service_account, monkeypatch):
    from oddish.workers.harbor import model_hosts

    env = vertex_ai.vertex_ai_agent_env(service_account, None)
    assert model_hosts.outbound_hosts_for_model(
        "vertex_ai/claude-sonnet-5", agent_env=env
    ) == [
        "aiplatform.googleapis.com",
        "oauth2.googleapis.com",
    ]
    regional = dict(env, GOOGLE_CLOUD_LOCATION="europe-west1")
    assert model_hosts.outbound_hosts_for_model(
        "vertex/gemini-3.8-flash", agent_env=regional
    )[0] == ("europe-west1-aiplatform.googleapis.com")
    # Read paths with no env fall back to the configured provider defaults.
    monkeypatch.setattr(settings, "vertex_ai_location", "us-east5")
    assert model_hosts.outbound_hosts_for_model("vertex_ai/gemini-3.8-flash") == [
        "us-east5-aiplatform.googleapis.com",
        "oauth2.googleapis.com",
    ]
    assert model_hosts.gemini_cli_transport_hosts(
        env, model_name="vertex_ai/gemini-3.8-flash"
    ) == ["aiplatform.googleapis.com", "oauth2.googleapis.com"]
    assert model_hosts.gemini_cli_transport_hosts({}) == [
        "generativelanguage.googleapis.com"
    ]


def test_restricted_gemini_profile_accepts_only_the_marked_profile(
    service_account, monkeypatch
):
    from oddish.workers.harbor.restricted_network import (
        RestrictedNetworkProfileError,
        restricted_network_profile_for_config,
    )

    for key in (
        "GEMINI_FORCE_OAUTH",
        "GEMINI_OAUTH_CREDS_PATH",
        "GOOGLE_GENAI_USE_VERTEXAI",
        "GOOGLE_GEMINI_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    config = AgentConfig(
        import_path="oddish.workers.agents.gemini_cli:OddishGeminiCli",
        model_name="vertex_ai/gemini-3.8-flash",
    )
    profile_env = vertex_ai.vertex_ai_agent_env(service_account, config.model_name)
    profile = restricted_network_profile_for_config(config, resolved_env=profile_env)
    assert profile.outbound_hosts == (
        "aiplatform.googleapis.com",
        "oauth2.googleapis.com",
    )
    with pytest.raises(RestrictedNetworkProfileError):
        restricted_network_profile_for_config(
            config, resolved_env={"GOOGLE_GENAI_USE_VERTEXAI": "true"}
        )
    with pytest.raises(RestrictedNetworkProfileError, match="custom Gemini base URL"):
        restricted_network_profile_for_config(
            config,
            resolved_env=dict(
                profile_env, GOOGLE_GEMINI_BASE_URL="https://relay.example"
            ),
        )


def test_submitted_marker_cannot_widen_a_non_vertex_trial(service_account, monkeypatch):
    """The marker is Oddish-owned: it counts only with a canonical vertex_ai/ id."""
    from oddish.workers.harbor import model_hosts
    from oddish.workers.harbor.restricted_network import (
        RestrictedNetworkProfileError,
        restricted_network_profile_for_config,
    )

    for key in (
        "GEMINI_FORCE_OAUTH",
        "GEMINI_OAUTH_CREDS_PATH",
        "GOOGLE_GENAI_USE_VERTEXAI",
    ):
        monkeypatch.delenv(key, raising=False)
    spoofed = {
        "GOOGLE_GENAI_USE_VERTEXAI": "true",
        "ODDISH_VERTEX_AI_MODE": "service_account",
        "GOOGLE_CLOUD_LOCATION": "global",
    }
    config = AgentConfig(
        import_path="oddish.workers.agents.gemini_cli:OddishGeminiCli",
        model_name="gemini/gemini-3.8-flash",
    )
    with pytest.raises(RestrictedNetworkProfileError):
        restricted_network_profile_for_config(config, resolved_env=spoofed)
    assert model_hosts.gemini_cli_transport_hosts(
        spoofed, model_name="gemini/gemini-3.8-flash"
    ) == ["generativelanguage.googleapis.com"]
    assert model_hosts.outbound_hosts_for_model(
        "gemini/gemini-3.8-flash", agent_env=spoofed
    ) == ["generativelanguage.googleapis.com"]
    # A submitted marker on a real Vertex trial is overwritten by the worker's own.
    built = _build(
        "gemini-cli",
        "vertex_ai/gemini-3.8-flash",
        env={"ODDISH_VERTEX_AI_MODE": "api_key"},
    )
    assert built.env["ODDISH_VERTEX_AI_MODE"] == "service_account"


def test_gateway_routed_trial_gets_no_credential_plan(service_account):
    gateway = {"ODDISH_QA_MODEL_ROUTED": "1", "ANTHROPIC_MODEL": "claude-sonnet-5"}
    assert (
        vertex_ai.plan_for_model("vertex_ai/claude-sonnet-5", extra_agent_env=gateway)
        is None
    )
    assert (
        vertex_ai.plan_for_model("vertex_ai/claude-sonnet-5", extra_agent_env={})
        is not None
    )


def test_restricted_antigravity_profile_accepts_the_marked_profile(service_account):
    from oddish.workers.agents.antigravity_cli import OddishAntigravityCli
    from oddish.workers.harbor.model_hosts import ANTIGRAVITY_STARTUP_HOSTS
    from oddish.workers.harbor.restricted_network import (
        RestrictedNetworkProfileError,
        _antigravity_profile,
    )

    config = AgentConfig(
        import_path="oddish.workers.agents.antigravity_cli:OddishAntigravityCli",
        model_name="vertex_ai/gemini-3.8-flash",
    )
    profile = _antigravity_profile(
        OddishAntigravityCli,
        config,
        vertex_ai.vertex_ai_agent_env(service_account, config.model_name),
    )
    assert profile.outbound_hosts[0] == "aiplatform.googleapis.com"
    for host in ANTIGRAVITY_STARTUP_HOSTS:
        assert host in profile.outbound_hosts
    with pytest.raises(RestrictedNetworkProfileError, match="Vertex routing"):
        _antigravity_profile(
            OddishAntigravityCli, config, {"GOOGLE_GENAI_USE_VERTEXAI": "true"}
        )


def test_claude_profile_keeps_the_token_host_with_a_vertex_override(service_account):
    from oddish.workers.agents.claude_code import OddishClaudeCode
    from oddish.workers.harbor.restricted_network import _claude_profile

    config = AgentConfig(
        import_path="oddish.workers.agents.claude_code:OddishClaudeCode",
        model_name="vertex_ai/claude-sonnet-5",
    )
    env = vertex_ai.vertex_ai_agent_env(service_account, config.model_name)
    assert _claude_profile(OddishClaudeCode, config, env).outbound_hosts == (
        "aiplatform.googleapis.com",
        "oauth2.googleapis.com",
    )
    override = dict(env, ANTHROPIC_VERTEX_BASE_URL="https://vertex-gateway.example")
    assert _claude_profile(OddishClaudeCode, config, override).outbound_hosts == (
        "vertex-gateway.example",
        "oauth2.googleapis.com",
    )


def test_vertex_hosts_are_allowlisted_on_every_restricted_shape(
    service_account, monkeypatch
):
    """No-network tasks: the Vertex endpoint and token host reach the allowlist.

    Three shapes carry a network policy: the Modal/Daytona single-container
    restricted agent phase (extra_allowed_hosts), the legacy
    ``allow_internet=false`` install arms (environment baseline), and the
    Daytona Compose class profiles. All three resolve the same two hosts from
    the profile Oddish published, for every harness alike.
    """
    from harbor.utils.env import resolve_env_vars

    from oddish.workers.harbor import runner as harbor_runner
    from oddish.workers.harbor.restricted_network import (
        restricted_network_profile_for_config,
    )

    vertex_hosts = ["aiplatform.googleapis.com", "oauth2.googleapis.com"]

    # Single-container restricted agent phase.
    monkeypatch.setattr(
        harbor_runner, "_supports_auto_restricted_agent_network", lambda **_: True
    )
    for agent, model in (
        ("claude-code", "vertex_ai/claude-sonnet-5"),
        ("gemini-cli", "vertex_ai/gemini-3.8-flash"),
        ("mini-swe-agent", "vertex_ai/gemini-3.8-flash"),
        ("opencode", "vertex_ai/gemini-3.8-flash"),
    ):
        built = _build(agent, model)
        harbor_runner._inject_restricted_agent_model_hosts(
            task_path=Path("/nonexistent"),
            environment_config=harbor_runner.HarborEnvironmentConfig(type="modal"),
            agent_config=built,
        )
        assert built.extra_allowed_hosts == vertex_hosts, agent

    # Legacy closed tasks: the install arms carry the model host.
    assert (
        harbor_runner._claude_code_environment_hosts(
            _build("claude-code", "vertex_ai/claude-sonnet-5")
        )[-2:]
        == vertex_hosts
    )
    assert (
        harbor_runner._gemini_cli_environment_hosts(
            _build("gemini-cli", "vertex_ai/gemini-3.8-flash")
        )[-2:]
        == vertex_hosts
    )
    assert (
        harbor_runner._opencode_environment_hosts(
            _build("opencode", "vertex_ai/gemini-3.8-flash")
        )[-2:]
        == vertex_hosts
    )

    # Daytona Compose class profiles.
    for agent, model in (
        ("claude-code", "vertex_ai/claude-sonnet-5"),
        ("gemini-cli", "vertex_ai/gemini-3.8-flash"),
        ("mini-swe-agent", "vertex_ai/gemini-3.8-flash"),
    ):
        built = _build(agent, model)
        harbor_runner._apply_gemini_cli_oddish_wrapper(built)
        profile = restricted_network_profile_for_config(
            built, resolved_env=resolve_env_vars(built.env)
        )
        assert profile.outbound_hosts == tuple(vertex_hosts), agent


def test_express_mode_allowlists_only_the_global_endpoint(express, monkeypatch):
    from harbor.utils.env import resolve_env_vars

    from oddish.workers.harbor import model_hosts, runner as harbor_runner
    from oddish.workers.harbor.restricted_network import (
        restricted_network_profile_for_config,
    )

    # The runner makes the key ambient before templates resolve; mirror that.
    monkeypatch.setenv("VERTEX_AI_API_KEY", "express-key")
    built = _build("gemini-cli", "vertex_ai/gemini-3.8-flash")
    env = resolve_env_vars(built.env)
    assert env["GOOGLE_API_KEY"] == "express-key"
    assert model_hosts.outbound_hosts_for_model(built.model_name, agent_env=env) == [
        "aiplatform.googleapis.com"
    ]
    harbor_runner._apply_gemini_cli_oddish_wrapper(built)
    profile = restricted_network_profile_for_config(built, resolved_env=env)
    assert profile.outbound_hosts == ("aiplatform.googleapis.com",)


# --- accounting -------------------------------------------------------------------


def test_job_scoped_bundle_carries_coordinates_only(service_account):
    from oddish.workers.queue.job_tokens import scoped_model_env

    env = scoped_model_env(
        agent="gemini-cli", model="vertex_ai/gemini-3.8-flash", settings=settings
    )
    assert env == {
        "VERTEXAI_LOCATION": "global",
        "GOOGLE_CLOUD_LOCATION": "global",
        "VERTEXAI_PROJECT": "oddish-vertex",
        "GOOGLE_CLOUD_PROJECT": "oddish-vertex",
    }


def test_fingerprint_hashes_the_settings_credential(service_account, monkeypatch):
    from oddish.core.llm_key_fingerprint import (
        hash_llm_key,
        platform_key_hash_for_provider,
        provider_key_var,
        trial_llm_key_hash,
    )

    monkeypatch.delenv("VERTEX_AI_CREDENTIALS_JSON", raising=False)
    assert provider_key_var("vertex_ai") == "VERTEX_AI_CREDENTIALS_JSON"
    assert platform_key_hash_for_provider("vertex_ai") == hash_llm_key(_SA_JSON)
    assert trial_llm_key_hash("vertex_ai", None) == hash_llm_key(_SA_JSON)
    monkeypatch.setattr(settings, "vertex_ai_credentials_json", None)
    monkeypatch.setattr(settings, "vertex_ai_api_key", None)
    assert platform_key_hash_for_provider("vertex_ai") is None


def test_pricing_resolves_vertex_ids_and_falls_back_past_the_at_date():
    from oddish.model_pricing import _spelling_variants, get_model_pricing

    assert "claude-sonnet-4-5" in _spelling_variants("claude-sonnet-4-5@20250929")
    assert get_model_pricing("vertex_ai/gemini-3.8-flash") is not None
    assert get_model_pricing("vertex_ai/claude-sonnet-4-5@20250929") is not None


def test_verifier_route_for_vertex_claude_carries_no_key_hash():
    from oddish.costs.verifier_cost import (
        ROUTE_ANTHROPIC,
        ROUTE_OTHER,
        infer_verifier_route,
    )

    assert infer_verifier_route("vertex_ai/claude-sonnet-5") == ROUTE_OTHER
    assert infer_verifier_route("google-vertex/claude-sonnet-5") == ROUTE_OTHER
    assert infer_verifier_route("anthropic/claude-sonnet-5") == ROUTE_ANTHROPIC


def test_ephemeral_payload_carries_the_plan_but_never_the_worker_path_in_runtime_env(
    service_account, express, tmp_path, monkeypatch
):
    """The override-Harbor child gets the process env and hook plan by name.

    ``runtime_env`` is merged OVER the agent env by the child, so the worker
    credential path must never ride it; it travels in the ``vertex_ai`` field
    the child applies to its own ``os.environ``.
    """
    from harbor.models.environment_type import EnvironmentType
    from harbor.models.trial.config import EnvironmentConfig

    from oddish.workers.harbor import ephemeral

    monkeypatch.setattr(vertex_ai.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        ephemeral, "_supports_auto_restricted_agent_network", lambda **_: False
    )

    def build(agent: str, model: str) -> dict:
        return ephemeral._build_payload(
            task_path=tmp_path / "task",
            jobs_dir=tmp_path / "jobs",
            outcome_path=tmp_path / "outcome.json",
            agent=agent,
            model=model,
            environment_config=EnvironmentConfig(type=EnvironmentType.MODAL),
            raw_harbor_config={"agent_config": {"name": agent, "model_name": model}},
            is_probe=False,
        )

    monkeypatch.setattr(settings, "vertex_ai_credentials_json", SecretStr(_SA_JSON))
    monkeypatch.setattr(settings, "vertex_ai_project_id", "oddish-vertex")
    monkeypatch.setattr(settings, "vertex_ai_api_key", None)
    payload = build("claude-code", "vertex_ai/claude-sonnet-5")
    plan = payload["vertex_ai"]
    assert (
        plan["worker_path"]
        and plan["process_env"]["VERTEXAI_CREDENTIALS"] == plan["worker_path"]
    )
    assert plan["process_env"]["CLAUDE_CODE_USE_BEDROCK"] == ""
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in payload["runtime_env"]
    assert payload["runtime_env"]["CLAUDE_CODE_USE_BEDROCK"] == ""
    assert payload["agent_config"]["env"]["GOOGLE_APPLICATION_CREDENTIALS"] == (
        "/tmp/oddish-vertex/service-account.json"
    )
    assert payload["model"] == "vertex_ai/claude-sonnet-5"
    assert build("claude-code", "anthropic-hdo/claude-sonnet-5")["vertex_ai"] is None

    monkeypatch.setattr(settings, "vertex_ai_credentials_json", None)
    monkeypatch.setattr(settings, "vertex_ai_api_key", SecretStr("express-key"))
    plan = build("gemini-cli", "vertex_ai/gemini-3.8-flash")["vertex_ai"]
    assert plan["worker_path"] is None
    assert plan["process_env"]["VERTEX_AI_API_KEY"] == "express-key"


def test_ephemeral_runtime_env_blanks_bedrock_for_vertex(service_account):
    from oddish.workers.harbor.ephemeral import _runtime_env_overrides

    env = _runtime_env_overrides(
        agent="claude-code",
        model="vertex_ai/claude-sonnet-5",
        raw_harbor_config={},
        is_probe=False,
    )
    assert (
        env["CLAUDE_CODE_USE_BEDROCK"] == "" and env["AWS_BEARER_TOKEN_BEDROCK"] == ""
    )
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in env
