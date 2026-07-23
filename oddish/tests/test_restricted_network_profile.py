from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from typing import Any

import pytest
from harbor.models.environment_type import EnvironmentType
from harbor.models.trial.config import (
    AgentConfig,
    EnvironmentConfig as HarborEnvironmentConfig,
)
from harbor.agents.installed.gemini_cli import GeminiCli

from oddish.workers.harbor import agent_config as agent_config_builder
from oddish.workers.harbor import runner
from oddish.workers.agents.codex import OddishCodex
from oddish.workers.agents.gemini_cli import OddishGeminiCli
from oddish.workers.harbor.restricted_network import (
    RUNTIME_ALLOWED_HOSTS_ATTR,
    RestrictedNetworkProfile,
    RestrictedNetworkProfileError,
    agent_fronts_own_model_service,
    apply_restricted_network_profile,
    assert_no_serialized_restricted_routes,
    reject_submitted_restricted_routes,
    resolve_effective_agent_class,
    restricted_network_profile_for_config,
)


class EmptyTransportAgent:
    @classmethod
    def restricted_network_profile(
        cls,
        *,
        model_name: str | None,
        env: Mapping[str, str],
        kwargs: Mapping[str, Any],
    ) -> RestrictedNetworkProfile:
        return RestrictedNetworkProfile(server_web_disabled=True)


class CompositeTransportAgent:
    @classmethod
    def restricted_network_profile(
        cls,
        *,
        model_name: str | None,
        env: Mapping[str, str],
        kwargs: Mapping[str, Any],
    ) -> dict[str, Any]:
        # A custom harness can declare both its relay and its provider. The
        # resolver treats this as one complete union; it does not choose one by
        # trial-facing agent/model aliases.
        return {
            "outbound_hosts": ["harness.test", "provider.test"],
            "env_overrides": {"REMOTE_WEB_DISABLED": "1"},
            "server_web_disabled": True,
        }


class UnknownAgent:
    pass


class UnsafeAgent:
    @classmethod
    def restricted_network_profile(cls, **_: Any) -> RestrictedNetworkProfile:
        return RestrictedNetworkProfile(outbound_hosts=("model.test",))


class InheritedCodexWithoutLocalAttestation(OddishCodex):
    pass


class ContextCapturingAgent:
    seen_env: Mapping[str, str] | None = None
    seen_kwargs: Mapping[str, Any] | None = None

    @classmethod
    def restricted_network_profile(
        cls,
        *,
        model_name: str | None,
        env: Mapping[str, str],
        kwargs: Mapping[str, Any],
    ) -> RestrictedNetworkProfile:
        cls.seen_env = env
        cls.seen_kwargs = kwargs
        return RestrictedNetworkProfile(server_web_disabled=True)


def _import_path(agent_class: type[Any]) -> str:
    return f"{__name__}:{agent_class.__name__}"


def _restricted_task(tmp_path):
    task_path = tmp_path / "task"
    environment = task_path / "environment"
    environment.mkdir(parents=True)
    (environment / "docker-compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (task_path / "task.toml").write_text(
        """schema_version = "1.3"

[environment]
network_mode = "public"

[agent]
network_mode = "no-network"
""",
        encoding="utf-8",
    )
    return task_path


@pytest.mark.parametrize(
    ("raw_config", "expected_field"),
    [
        (
            {"agent_config": {"extra_allowed_hosts": ["private-route.test"]}},
            "agent_config.extra_allowed_hosts",
        ),
        (
            {"agent_config": {"env": {"OPENAI_BASE_URL": "${PRIVATE_MODEL_ROUTE}"}}},
            "agent_config.env.OPENAI_BASE_URL",
        ),
        (
            {
                "agent_config": {
                    "kwargs": {
                        "extra_env": {
                            "ANTHROPIC_BASE_URL": "https://private-route.test"
                        }
                    }
                }
            },
            "agent_config.kwargs.extra_env.ANTHROPIC_BASE_URL",
        ),
        (
            {
                "agent_overrides": {
                    "env": {"GOOGLE_GEMINI_BASE_URL": "private-route.test"}
                }
            },
            "agent_overrides.env.GOOGLE_GEMINI_BASE_URL",
        ),
        (
            {
                "agent_overrides": {
                    "kwargs": {
                        "extra_env": {"CURSOR_API_ENDPOINT": "private-route.test"}
                    }
                }
            },
            "agent_overrides.kwargs.extra_env.CURSOR_API_ENDPOINT",
        ),
        (
            {"agent_overrides": {"extra_allowed_hosts": ["private-route.test"]}},
            "agent_overrides.extra_allowed_hosts",
        ),
    ],
)
def test_submitted_restricted_routes_are_rejected_without_echoing_values(
    raw_config, expected_field
) -> None:
    with pytest.raises(RestrictedNetworkProfileError) as exc_info:
        reject_submitted_restricted_routes(raw_config)

    message = str(exc_info.value)
    assert expected_field in message
    assert "private-route.test" not in message
    assert "${PRIVATE_MODEL_ROUTE}" not in message


def test_submitted_byok_credential_is_not_treated_as_a_route() -> None:
    reject_submitted_restricted_routes(
        {
            "agent_config": {
                "env": {
                    "ANTHROPIC_API_KEY": "private-byok-value",
                    "OPENAI_API_KEY": "private-byok-value",
                }
            },
            "agent_overrides": {"env": {"FIREWORKS_API_KEY": "private-byok-value"}},
        }
    )


def test_effective_restricted_config_must_not_serialize_routes() -> None:
    config = AgentConfig(name="nop", extra_allowed_hosts=["route.test"])

    with pytest.raises(
        RestrictedNetworkProfileError,
        match="non-attested extra_allowed_hosts after build",
    ):
        assert_no_serialized_restricted_routes(config)


def test_explicit_empty_profile_is_valid() -> None:
    config = AgentConfig(import_path=_import_path(EmptyTransportAgent))

    profile = restricted_network_profile_for_config(config, resolved_env={})

    assert profile.outbound_hosts == ()
    assert profile.server_web_disabled is True


@pytest.mark.parametrize("agent_name", ["nop", "oracle"])
def test_integrity_agents_remain_transport_free_in_restricted_compose(
    tmp_path, agent_name
) -> None:
    config = AgentConfig(name=agent_name)

    profile = runner._apply_restricted_agent_network_defaults(
        task_path=_restricted_task(tmp_path),
        environment_config=HarborEnvironmentConfig(type=EnvironmentType.DAYTONA),
        agent_config=config,
    )

    assert profile is not None
    assert profile.outbound_hosts == ()
    assert config.extra_allowed_hosts == []
    assert getattr(config, RUNTIME_ALLOWED_HOSTS_ATTR) == ()


def test_unknown_custom_agent_fails_closed() -> None:
    config = AgentConfig(import_path=_import_path(UnknownAgent))

    with pytest.raises(RestrictedNetworkProfileError, match="does not declare"):
        restricted_network_profile_for_config(config, resolved_env={})


def test_subclass_cannot_inherit_a_trusted_stock_profile() -> None:
    config = AgentConfig(
        import_path=_import_path(InheritedCodexWithoutLocalAttestation),
        model_name="openai/model",
    )

    with pytest.raises(RestrictedNetworkProfileError, match="does not declare"):
        restricted_network_profile_for_config(config, resolved_env={})


def test_custom_hook_context_excludes_credentials_recursively() -> None:
    config = AgentConfig(
        import_path=_import_path(ContextCapturingAgent),
        kwargs={
            "mode": "safe",
            "api_key": "kwarg-secret",
            "extra_env": {
                "OPENAI_BASE_URL": "https://model.test/v1",
                "OPENAI_API_KEY": "nested-secret",
            },
        },
    )

    restricted_network_profile_for_config(
        config,
        resolved_env={
            "OPENAI_BASE_URL": "https://model.test/v1",
            "OPENAI_API_KEY": "env-secret",
        },
    )

    assert ContextCapturingAgent.seen_env == {
        "OPENAI_BASE_URL": "https://model.test/v1"
    }
    assert ContextCapturingAgent.seen_kwargs == {
        "mode": "safe",
        "extra_env": {"OPENAI_BASE_URL": "https://model.test/v1"},
    }


def test_profile_without_web_containment_fails_closed() -> None:
    config = AgentConfig(import_path=_import_path(UnsafeAgent))

    with pytest.raises(RestrictedNetworkProfileError, match="server-side web"):
        restricted_network_profile_for_config(config, resolved_env={})


def test_custom_profile_composes_harness_and_provider_hosts() -> None:
    config = AgentConfig(
        import_path=_import_path(CompositeTransportAgent),
        extra_allowed_hosts=["task-internal.test"],
    )

    profile = apply_restricted_network_profile(agent_config=config, resolved_env={})

    assert profile.outbound_hosts == ("harness.test", "provider.test")
    assert config.extra_allowed_hosts == [
        "task-internal.test",
        "harness.test",
        "provider.test",
    ]
    assert config.env["REMOTE_WEB_DISABLED"] == "1"


def test_restricted_security_overrides_cannot_be_reenabled() -> None:
    config = AgentConfig(
        name="codex",
        model_name="custom",
        env={"OPENAI_BASE_URL": "https://model.test/v1"},
        kwargs={"web_search": "live"},
    )

    apply_restricted_network_profile(
        agent_config=config,
        resolved_env={"OPENAI_BASE_URL": "https://model.test/v1"},
    )

    assert config.kwargs["web_search"] == "disabled"


def test_restricted_codex_rejects_irrelevant_base_url_without_leaking_value() -> None:
    irrelevant_url = "https://private-anthropic-route.test/secret/path"
    config = AgentConfig(
        import_path="oddish.workers.agents.codex:OddishCodex",
        model_name="openai/model",
    )

    with pytest.raises(RestrictedNetworkProfileError) as exc_info:
        restricted_network_profile_for_config(
            config,
            resolved_env={
                "OPENAI_BASE_URL": "https://selected-openai-route.test/v1",
                "ANTHROPIC_BASE_URL": irrelevant_url,
            },
        )

    message = str(exc_info.value)
    assert "ANTHROPIC_BASE_URL" in message
    assert irrelevant_url not in message


def test_restricted_codex_rejects_conflicting_selected_aliases() -> None:
    config = AgentConfig(
        import_path="oddish.workers.agents.codex:OddishCodex",
        model_name="openai/model",
    )

    with pytest.raises(RestrictedNetworkProfileError, match="conflicting aliases"):
        restricted_network_profile_for_config(
            config,
            resolved_env={
                "OPENAI_BASE_URL": "https://selected-openai-route.test/v1",
                "OPENAI_API_BASE": "https://other-openai-route.test/v1",
            },
        )


def test_restricted_meta_mini_swe_accepts_meta_transport_route() -> None:
    config = AgentConfig(
        import_path="oddish.workers.agents.mini_swe_agent:OddishMetaMiniSweAgent",
        model_name="meta/model",
        env={"META_BASE_URL": "https://selected-meta-route.test/v1"},
    )

    profile = restricted_network_profile_for_config(
        config,
        resolved_env=config.env,
    )

    assert profile.outbound_hosts == ("selected-meta-route.test",)


@pytest.mark.parametrize("agent_name", ["nop", "oracle"])
def test_transport_free_agents_do_not_inherit_worker_base_urls(agent_name) -> None:
    config = AgentConfig(name=agent_name)

    profile = restricted_network_profile_for_config(
        config,
        resolved_env={
            "OPENAI_BASE_URL": "https://irrelevant-openai-route.test/v1",
            "ANTHROPIC_BASE_URL": "https://irrelevant-anthropic-route.test/v1",
        },
    )

    assert profile.outbound_hosts == ()


def test_gemini_wrapper_is_selected_only_for_daytona_compose(tmp_path) -> None:
    config = agent_config_builder._build_agent_config(
        agent="gemini-cli",
        model="google/gemini-test",
        raw_harbor_config={},
    )

    effective = resolve_effective_agent_class(config)

    assert effective.__module__ == "harbor.agents.installed.gemini_cli"
    assert effective.__name__ == "GeminiCli"

    runner._apply_restricted_agent_network_defaults(
        task_path=_restricted_task(tmp_path),
        environment_config=HarborEnvironmentConfig(type=EnvironmentType.DAYTONA),
        agent_config=config,
    )
    effective = resolve_effective_agent_class(config)

    assert effective.__module__ == "oddish.workers.agents.gemini_cli"
    assert effective.__name__ == "OddishGeminiCli"


def test_gemini_wrapper_removes_remote_web_tools(tmp_path) -> None:
    agent = OddishGeminiCli(
        logs_dir=tmp_path,
        model_name="google/gemini-test",
        disable_web_tools=True,
    )

    config, _ = agent._build_settings_config()

    assert config is not None
    assert config["tools"]["exclude"] == ["google_web_search", "web_fetch"]


def test_gemini_restricted_profile_uses_exact_runtime_route(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_FORCE_OAUTH", raising=False)
    monkeypatch.delenv("GEMINI_OAUTH_CREDS_PATH", raising=False)
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
    config = AgentConfig(
        import_path="oddish.workers.agents.gemini_cli:OddishGeminiCli",
        model_name="google/model",
    )

    profile = apply_restricted_network_profile(
        agent_config=config,
        resolved_env={"GOOGLE_GEMINI_BASE_URL": "https://gemini-relay.test/v1"},
    )

    assert profile.outbound_hosts == ("gemini-relay.test",)
    assert config.env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] == (
        "/etc/gemini-cli/settings.json"
    )


@pytest.mark.parametrize(
    "resolved_env",
    [
        {"GEMINI_FORCE_OAUTH": "true"},
        {"GEMINI_OAUTH_CREDS_PATH": "/private/oauth.json"},
        {"GOOGLE_GENAI_USE_VERTEXAI": "true"},
    ],
)
def test_gemini_unbounded_transports_fail_closed(monkeypatch, resolved_env) -> None:
    for key in (
        "GEMINI_FORCE_OAUTH",
        "GEMINI_OAUTH_CREDS_PATH",
        "GOOGLE_GENAI_USE_VERTEXAI",
        "GOOGLE_GEMINI_BASE_URL",
        "GEMINI_API_BASE_URL",
        "GOOGLE_API_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    config = AgentConfig(
        import_path="oddish.workers.agents.gemini_cli:OddishGeminiCli",
        model_name="google/model",
    )

    with pytest.raises(RestrictedNetworkProfileError):
        restricted_network_profile_for_config(config, resolved_env=resolved_env)


@pytest.mark.asyncio
async def test_gemini_installs_authoritative_system_web_tool_policy(
    monkeypatch, tmp_path
) -> None:
    commands: list[str] = []

    async def _base_install(self, environment) -> None:
        return None

    async def _capture_root(self, environment, command, **kwargs) -> None:
        commands.append(command)

    monkeypatch.setattr(GeminiCli, "install", _base_install)
    monkeypatch.setattr(OddishGeminiCli, "exec_as_root", _capture_root)
    agent = OddishGeminiCli(
        logs_dir=tmp_path,
        model_name="google/model",
        disable_web_tools=True,
    )

    await agent.install(object())

    assert len(commands) == 1
    command = commands[0]
    assert "/etc/gemini-cli/settings.json" in command
    assert "google_web_search" in command
    assert "web_fetch" in command
    assert "chmod 0444" in command


@pytest.mark.parametrize(
    "config",
    [
        AgentConfig(name="nop"),
        AgentConfig(name="oracle"),
        AgentConfig(
            import_path="oddish.workers.agents.claude_code:OddishClaudeCode",
            model_name="anthropic/model",
        ),
        AgentConfig(
            import_path="oddish.workers.agents.codex:OddishCodex",
            model_name="openai/model",
        ),
        AgentConfig(
            import_path="oddish.workers.agents.grok_build:OddishGrokBuild",
            model_name="xai/model",
        ),
        AgentConfig(
            import_path="oddish.workers.agents.mini_swe_agent:OddishMiniSweAgent",
            model_name="anthropic/model",
        ),
        AgentConfig(
            import_path="oddish.workers.agents.mini_swe_agent:OddishMetaMiniSweAgent",
            model_name="meta/model",
            env={"OPENAI_BASE_URL": "https://model.test/v1"},
        ),
        AgentConfig(
            import_path="oddish.workers.agents.gemini_cli:OddishGeminiCli",
            model_name="google/model",
        ),
        AgentConfig(name="cursor-cli", model_name="cursor/model"),
    ],
)
def test_every_operational_effective_class_has_a_safe_profile(config) -> None:
    profile = restricted_network_profile_for_config(
        config,
        resolved_env=config.env,
    )

    assert profile.server_web_disabled is True


def test_unknown_agent_rejected_before_runner_can_construct_job(tmp_path) -> None:
    task_path = _restricted_task(tmp_path)
    config = AgentConfig(import_path=_import_path(UnknownAgent))

    with pytest.raises(RestrictedNetworkProfileError):
        runner._apply_restricted_agent_network_defaults(
            task_path=task_path,
            environment_config=HarborEnvironmentConfig(type=EnvironmentType.DAYTONA),
            agent_config=config,
        )


def test_unknown_agent_fails_before_job_create(monkeypatch, tmp_path) -> None:
    task_path = _restricted_task(tmp_path)
    job_create_called = False

    class SentinelJob:
        @classmethod
        async def create(cls, config):
            nonlocal job_create_called
            job_create_called = True
            raise AssertionError("Job.create must not run")

    monkeypatch.setattr(runner, "Job", SentinelJob)
    monkeypatch.setattr(runner, "get_backend", lambda value: None)
    monkeypatch.setattr(runner, "validate_task_timeout_config", lambda path: None)
    monkeypatch.setattr(
        runner, "_check_local_storage_preflight", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        runner,
        "_build_agent_config",
        lambda **kwargs: AgentConfig(import_path=_import_path(UnknownAgent)),
    )
    monkeypatch.setattr(runner, "_trial_uses_openai_provider", lambda **kwargs: False)

    outcome = asyncio.run(
        runner.run_harbor_trial_async(
            task_path=task_path,
            agent="custom",
            jobs_dir=tmp_path / "jobs",
            environment=EnvironmentType.DAYTONA,
        )
    )

    assert job_create_called is False
    assert outcome.exception_type == "RestrictedNetworkProfileError"
    assert "does not declare" in (outcome.error or "")


def test_submitted_route_fails_before_agent_build_or_job_create(
    monkeypatch, tmp_path
) -> None:
    task_path = _restricted_task(tmp_path)
    agent_build_called = False
    job_create_called = False

    def _unexpected_agent_build(**kwargs):
        nonlocal agent_build_called
        agent_build_called = True
        raise AssertionError("agent build must not run")

    class SentinelJob:
        @classmethod
        async def create(cls, config):
            nonlocal job_create_called
            job_create_called = True
            raise AssertionError("Job.create must not run")

    monkeypatch.setattr(runner, "apply_harbor_patches", lambda: None)
    monkeypatch.setattr(runner, "Job", SentinelJob)
    monkeypatch.setattr(runner, "get_backend", lambda value: None)
    monkeypatch.setattr(runner, "validate_task_timeout_config", lambda path: None)
    monkeypatch.setattr(
        runner, "_check_local_storage_preflight", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(runner, "_build_agent_config", _unexpected_agent_build)
    private_value = "https://must-not-appear.test/private"

    outcome = asyncio.run(
        runner.run_harbor_trial_async(
            task_path=task_path,
            agent="codex",
            jobs_dir=tmp_path / "jobs",
            environment=EnvironmentType.DAYTONA,
            harbor_config={"agent_config": {"env": {"OPENAI_BASE_URL": private_value}}},
        )
    )

    assert agent_build_called is False
    assert job_create_called is False
    assert outcome.exception_type == "RestrictedNetworkProfileError"
    assert "agent_config.env.OPENAI_BASE_URL" in (outcome.error or "")
    assert private_value not in (outcome.error or "")


def test_built_serialized_route_fails_before_job_create(monkeypatch, tmp_path) -> None:
    task_path = _restricted_task(tmp_path)
    job_create_called = False

    class SentinelJob:
        @classmethod
        async def create(cls, config):
            nonlocal job_create_called
            job_create_called = True
            raise AssertionError("Job.create must not run")

    monkeypatch.setattr(runner, "apply_harbor_patches", lambda: None)
    monkeypatch.setattr(runner, "Job", SentinelJob)
    monkeypatch.setattr(runner, "get_backend", lambda value: None)
    monkeypatch.setattr(runner, "validate_task_timeout_config", lambda path: None)
    monkeypatch.setattr(
        runner, "_check_local_storage_preflight", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        runner,
        "_build_agent_config",
        lambda **kwargs: AgentConfig(
            name="nop", extra_allowed_hosts=["unexpected-route.test"]
        ),
    )
    monkeypatch.setattr(runner, "_trial_uses_openai_provider", lambda **kwargs: False)

    outcome = asyncio.run(
        runner.run_harbor_trial_async(
            task_path=task_path,
            agent="nop",
            jobs_dir=tmp_path / "jobs",
            environment=EnvironmentType.DAYTONA,
        )
    )

    assert job_create_called is False
    assert outcome.exception_type == "RestrictedNetworkProfileError"
    assert "non-attested extra_allowed_hosts after build" in (outcome.error or "")
    assert "unexpected-route.test" not in (outcome.error or "")


def test_restricted_compose_ephemeral_variant_fails_before_dispatch(
    monkeypatch, tmp_path
) -> None:
    import oddish.workers.harbor.ephemeral as ephemeral

    task_path = _restricted_task(tmp_path)
    dispatched = False

    async def _unexpected_dispatch(**kwargs):
        nonlocal dispatched
        dispatched = True
        raise AssertionError("restricted ephemeral variant must not dispatch")

    monkeypatch.setattr(ephemeral, "run_ephemeral_harbor_trial", _unexpected_dispatch)
    monkeypatch.setattr(runner, "apply_harbor_patches", lambda: None)
    monkeypatch.setattr(runner, "get_backend", lambda value: None)

    outcome = asyncio.run(
        runner.run_harbor_trial_async(
            task_path=task_path,
            agent="nop",
            jobs_dir=tmp_path / "jobs",
            environment=EnvironmentType.DAYTONA,
            harbor_config={"variant_id": "ephemeral"},
        )
    )

    assert dispatched is False
    assert outcome.exception_type == "RestrictedNetworkProfileError"
    assert "ephemeral Harbor variants are not supported" in (outcome.error or "")


def test_static_restricted_compose_rejects_model_agent_before_job_create(
    monkeypatch, tmp_path
) -> None:
    task_path = _restricted_task(tmp_path)
    (task_path / "task.toml").write_text(
        """schema_version = "1.3"

[environment]
network_mode = "no-network"

[agent]
network_mode = "no-network"
""",
        encoding="utf-8",
    )
    job_create_called = False

    class SentinelJob:
        @classmethod
        async def create(cls, config):
            nonlocal job_create_called
            job_create_called = True
            raise AssertionError("Job.create must not run")

    monkeypatch.setattr(runner, "Job", SentinelJob)
    monkeypatch.setattr(runner, "apply_harbor_patches", lambda: None)
    monkeypatch.setattr(runner, "get_backend", lambda value: None)
    monkeypatch.setattr(runner, "validate_task_timeout_config", lambda path: None)
    monkeypatch.setattr(
        runner, "_check_local_storage_preflight", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        runner,
        "_build_agent_config",
        lambda **kwargs: AgentConfig(name="codex", model_name="openai/model"),
    )
    monkeypatch.setattr(runner, "_trial_uses_openai_provider", lambda **kwargs: False)

    outcome = asyncio.run(
        runner.run_harbor_trial_async(
            task_path=task_path,
            agent="codex",
            jobs_dir=tmp_path / "jobs",
            environment=EnvironmentType.DAYTONA,
        )
    )

    assert job_create_called is False
    assert outcome.exception_type == "RestrictedNetworkProfileError"
    assert "public environment baseline" in (outcome.error or "")


def test_public_task_does_not_require_profile(tmp_path) -> None:
    task_path = _restricted_task(tmp_path)
    (task_path / "task.toml").write_text(
        """schema_version = "1.3"

[environment]
network_mode = "public"

[agent]
network_mode = "public"
""",
        encoding="utf-8",
    )
    config = AgentConfig(import_path=_import_path(UnknownAgent))

    assert (
        runner._apply_restricted_agent_network_defaults(
            task_path=task_path,
            environment_config=HarborEnvironmentConfig(type=EnvironmentType.DAYTONA),
            agent_config=config,
        )
        is None
    )


def test_runner_policy_application_has_no_agent_or_model_branches() -> None:
    source = inspect.getsource(runner._apply_restricted_agent_network_defaults)
    source += inspect.getsource(
        runner._apply_daytona_compose_restricted_network_profile
    )

    assert "agent_config.name" not in source
    assert "agent_config.model_name" not in source
    assert "outbound_hosts_for_model" not in source
    assert "outbound_hosts_for_agent" not in source


def test_agent_fronts_own_model_service_identifies_self_fronting_harnesses():
    # Cursor routes the model through its own service and must keep the public
    # model identity (never the worker-private Azure deployment id). Agents that
    # talk to the provider directly (codex, mini-swe -- including a BARE openai
    # model id like "gpt-4o") do not front their own service, so they still get
    # the deployment swap.
    def cfg(name: str, model: str = "openai/gpt-5") -> AgentConfig:
        return AgentConfig(name=name, model_name=model)

    assert agent_fronts_own_model_service(cfg("cursor-cli")) is True
    assert agent_fronts_own_model_service(cfg("codex")) is False
    assert agent_fronts_own_model_service(cfg("mini-swe-agent")) is False
    assert agent_fronts_own_model_service(cfg("mini-swe-agent", "gpt-4o")) is False
    assert agent_fronts_own_model_service(cfg("claude-code", "anthropic/claude")) is False
    assert agent_fronts_own_model_service(cfg("grok-build", "xai/grok")) is False
    assert agent_fronts_own_model_service(cfg("gemini-cli", "google/gemini")) is False
    assert agent_fronts_own_model_service(cfg("nop")) is False


def test_mini_swe_bare_openai_model_gets_openai_transport():
    # A mini-swe trial on a BARE OpenAI model id (no provider prefix) must still
    # resolve its OpenAI transport keys AND a non-empty inferred host set.
    # Regression: prefix-only detection granted the restricted agent phase no
    # model egress at all.
    from oddish.workers.harbor.restricted_network import (
        consumed_transport_base_url_keys,
    )

    config = AgentConfig(name="mini-swe-agent", model_name="gpt-4o")
    assert "OPENAI_BASE_URL" in (consumed_transport_base_url_keys(config) or ())

    profile = restricted_network_profile_for_config(config, resolved_env={})
    assert "api.openai.com" in profile.outbound_hosts
