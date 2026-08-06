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
        AgentConfig(
            import_path="oddish.workers.agents.cursor_cli:OddishCursorCli",
            model_name="cursor/model",
        ),
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
    assert (
        agent_fronts_own_model_service(cfg("claude-code", "anthropic/claude")) is False
    )
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


def test_provider_aliases_resolve_transport_keys():
    # infer_model_provider_prefix normalizes provider aliases so
    # _model_transport_base_url_keys resolves them (claude -> anthropic,
    # vertex_ai / palm -> gemini, moonshotai -> moonshot).
    from oddish.workers.harbor.restricted_network import (
        _model_transport_base_url_keys,
    )

    assert _model_transport_base_url_keys("claude/sonnet") == ("ANTHROPIC_BASE_URL",)
    assert "GEMINI_API_BASE_URL" in _model_transport_base_url_keys("vertex_ai/gemini")
    assert "GEMINI_API_BASE_URL" in _model_transport_base_url_keys("palm/x")
    assert _model_transport_base_url_keys("moonshotai/kimi") == ("MOONSHOT_BASE_URL",)


def test_grok_profile_falls_back_to_xai_host():
    # grok-build always fronts xAI; the profile must never leave an empty
    # allowlist even when the model's transport route is not explicitly resolved.
    profile = restricted_network_profile_for_config(
        AgentConfig(
            import_path="oddish.workers.agents.grok_build:OddishGrokBuild",
            model_name="",
        ),
        resolved_env={},
    )
    assert profile.outbound_hosts == ("api.x.ai",)


def test_bare_bedrock_model_resolves_to_bedrock_provider():
    # Bare Bedrock ids (global.anthropic.*) must resolve to the bedrock provider
    # so the provider-driven credential fold covers the ambient AWS keys.
    from oddish.config import infer_model_provider_prefix

    assert infer_model_provider_prefix("global.anthropic.claude-sonnet-4") == "bedrock"


def test_grok_profile_is_transport_authoritative():
    # grok-build always fronts xAI; the profile must pin api.x.ai and NOT let a
    # model id that classifies as another provider substitute a different host.
    for model in ("grok-4", "xai/grok-4", "openai/gpt-5", "anthropic/claude"):
        profile = restricted_network_profile_for_config(
            AgentConfig(
                import_path="oddish.workers.agents.grok_build:OddishGrokBuild",
                model_name=model,
            ),
            resolved_env={},
        )
        assert profile.outbound_hosts == ("api.x.ai",), model


def test_submitted_environment_routes_are_rejected():
    # A caller-submitted environment allowlist or transport base URL must be
    # rejected too, so it cannot widen a restricted trial's egress via the
    # environment config; values are never echoed in the error.
    for raw, field, secret in (
        (
            {"environment": {"extra_allowed_hosts": ["private-route.test"]}},
            "environment.extra_allowed_hosts",
            "private-route.test",
        ),
        (
            {"environment": {"env": {"OPENAI_BASE_URL": "https://private.test/v1"}}},
            "environment.env.OPENAI_BASE_URL",
            "private.test",
        ),
    ):
        with pytest.raises(RestrictedNetworkProfileError) as exc_info:
            reject_submitted_restricted_routes(raw)
        assert field in str(exc_info.value)
        assert secret not in str(exc_info.value)

    # A legitimate restricted-Compose submission carries only resource/lifecycle
    # environment kwargs (no routes) and must pass unchanged.
    reject_submitted_restricted_routes(
        {
            "agent_config": {"name": "codex"},
            "environment": {
                "type": "daytona",
                "extra_allowed_hosts": [],
                "kwargs": {"auto_stop_interval_mins": 30, "ephemeral": False},
            },
        }
    )


def test_azure_transport_aliases_are_fail_closed_routes():
    """Azure aliases name the same OpenAI-family transport as OPENAI_BASE_URL.

    get_openai_agent_env emits AZURE_API_BASE / AZURE_OPENAI_ENDPOINT next to
    OPENAI_BASE_URL, so they must sit inside the fail-closed boundary: a caller
    cannot smuggle an unattested route in under an alias, and the worker's
    private Azure endpoint must not survive into an agent that fronts its own
    transport. They stay out of the public discovery tuple, which gains no host
    from them.
    """
    from oddish.workers.harbor import model_hosts

    for key in model_hosts.AZURE_BASE_URL_KEYS:
        assert key in model_hosts.KNOWN_TRANSPORT_BASE_URL_KEYS
        with pytest.raises(RestrictedNetworkProfileError) as excinfo:
            reject_submitted_restricted_routes(
                {"agent_config": {"env": {key: "https://attacker.example/v1"}}}
            )
        # The error names the field path, never the submitted value.
        assert key in str(excinfo.value)
        assert "attacker.example" not in str(excinfo.value)

    # Not in discovery: public-path allowlists are unchanged by this addition.
    assert set(model_hosts.AZURE_BASE_URL_KEYS).isdisjoint(
        model_hosts._BASE_URL_ENV_KEYS
    )


def test_azure_routed_openai_trial_still_resolves_one_host():
    """The fail-closed guard must not reject Oddish's default Azure routing.

    AZURE_OPENAI_ENDPOINT is the bare resource endpoint while OPENAI_BASE_URL /
    AZURE_API_BASE carry the ``/openai/v1`` suffix, so the aliases differ as
    strings but name one host. That is one transport, not a conflict.
    """
    from oddish.workers.harbor.restricted_network import (
        _selected_transport_hosts,
        consumed_transport_base_url_keys,
    )

    azure_base = "https://foo.openai.azure.com/openai/v1"
    worker_env = {
        "OPENAI_BASE_URL": azure_base,
        "AZURE_API_BASE": azure_base,
        "AZURE_OPENAI_ENDPOINT": "https://foo.openai.azure.com",
    }
    agent_config = AgentConfig(name="codex", model_name="openai/gpt-5.6-sol")
    hosts = _selected_transport_hosts(
        agent_config,
        worker_env,
        base_url_keys=consumed_transport_base_url_keys(agent_config),
    )
    assert hosts == ("foo.openai.azure.com",)


def test_conflicting_alias_hosts_still_fail_closed():
    """Relaxing the raw-string check must not weaken the boundary."""
    from oddish.workers.harbor.restricted_network import (
        _selected_transport_hosts,
        consumed_transport_base_url_keys,
    )

    agent_config = AgentConfig(name="codex", model_name="openai/gpt-5.6-sol")
    with pytest.raises(RestrictedNetworkProfileError):
        _selected_transport_hosts(
            agent_config,
            {
                "OPENAI_BASE_URL": "https://a.example/v1",
                "AZURE_API_BASE": "https://b.example/v1",
            },
            base_url_keys=consumed_transport_base_url_keys(agent_config),
        )


def test_self_fronting_agent_drops_worker_azure_route():
    """The runner filter must drop the worker's Azure route for cursor.

    Exercises _resolved_runtime_transport_env itself rather than asserting a
    property of the key registry, so a regression in the filter is caught.
    """
    from oddish.workers.harbor import runner as harbor_runner

    azure_base = "https://foo.openai.azure.com/openai/v1"
    worker_env = {
        "OPENAI_BASE_URL": azure_base,
        "AZURE_API_BASE": azure_base,
        "AZURE_OPENAI_ENDPOINT": "https://foo.openai.azure.com",
    }
    agent_config = AgentConfig(name="cursor-cli", model_name="cursor/composer")
    resolved = harbor_runner._resolved_runtime_transport_env(
        dict(worker_env), agent_config=agent_config
    )
    assert "AZURE_API_BASE" not in resolved
    assert "AZURE_OPENAI_ENDPOINT" not in resolved
    assert "OPENAI_BASE_URL" not in resolved


def test_alias_scheme_downgrade_and_unparseable_alias_fail_closed():
    """Path-only alias differences are fine; scheme/invalid ones are not.

    Comparing only the resolved host would let an ``http`` alias through (it
    ships the API key in cleartext) and would silently discard an alias that
    resolves to no host at all, leaving one apparently-unanimous route.
    """
    from oddish.workers.harbor.restricted_network import (
        _selected_transport_hosts,
        consumed_transport_base_url_keys,
    )

    agent_config = AgentConfig(name="codex", model_name="openai/gpt-5.6-sol")
    keys = consumed_transport_base_url_keys(agent_config)
    for bad_alias in ("http://h.example/v1", "https://"):
        with pytest.raises(RestrictedNetworkProfileError):
            _selected_transport_hosts(
                agent_config,
                {
                    "OPENAI_BASE_URL": "https://h.example/v1",
                    "AZURE_API_BASE": bad_alias,
                },
                base_url_keys=keys,
            )


def test_azure_and_vertex_model_ids_are_not_silently_transport_free():
    """A provider missing from the key map reads as 'needs no egress'.

    ``azure/`` and ``vertex_ai/`` are canonical providers, so they must resolve
    real transport keys and a real host instead of an empty set, which would
    attest a model-calling agent as deliberately transport-free.
    """
    from oddish.workers.harbor.restricted_network import (
        _selected_transport_hosts,
        consumed_transport_base_url_keys,
    )

    for model in ("azure/my-deployment", "azure_openai/my-deployment"):
        config = AgentConfig(name="mini-swe-agent", model_name=model)
        keys = consumed_transport_base_url_keys(config)
        assert "OPENAI_BASE_URL" in (keys or ())
        azure_base = "https://foo.openai.azure.com/openai/v1"
        assert _selected_transport_hosts(
            config,
            {"OPENAI_BASE_URL": azure_base, "AZURE_API_BASE": azure_base},
            base_url_keys=keys,
        ) == ("foo.openai.azure.com",)

    vertex = AgentConfig(name="mini-swe-agent", model_name="vertex_ai/gemini-2")
    assert _selected_transport_hosts(
        vertex, {}, base_url_keys=consumed_transport_base_url_keys(vertex)
    ) == ("generativelanguage.googleapis.com",)


def test_host_arms_never_outrun_the_transport_key_map():
    """A prefix that resolves a host must also resolve transport keys.

    The failure mode this pins is subtle: if a spelling resolves a HOST but an
    empty KEY set, the runner drops the operator's real (private) route and the
    inference fallback then grants the public host instead -- a silent route
    substitution *and* a widening. Every provider spelling the host switch
    matches must therefore also be carried by the model-transport key map.

    Scoped to the model-driven (litellm / mini-swe) transports. Harness-fronted
    agents such as cursor deliberately have no row in that map -- their keys
    come from the agent-class profile, which is what makes them
    transport-authoritative.
    """
    from oddish.workers.harbor import model_hosts
    from oddish.workers.harbor.restricted_network import (
        _model_transport_base_url_keys,
    )

    for model in (
        "openai/gpt-4o",
        "azure/my-deployment",
        "anthropic/claude-sonnet-5",
        "gemini/pro",
        "google/gemini-test",
        "vertex_ai/gemini-2.5-pro",
    ):
        hosts = model_hosts.outbound_hosts_for_model(model)
        keys = _model_transport_base_url_keys(model)
        if hosts:
            assert keys, f"{model} resolves hosts {hosts} but no transport keys"

    # A spelling the alias map does not carry must resolve NEITHER, so it can
    # never substitute a public host for the operator's private route.
    assert model_hosts.outbound_hosts_for_model("vertex-ai/gemini-2.5-pro") == []
    assert _model_transport_base_url_keys("vertex-ai/gemini-2.5-pro") == ()


def test_every_provider_alias_resolves_a_host_not_just_keys():
    """Exhaustive: an alias prefix must never yield keys with an empty allowlist.

    _model_transport_base_url_keys normalizes aliases to their canonical
    provider, so ``claude/`` resolves ANTHROPIC_BASE_URL and ``palm/`` the Gemini
    keys. Host resolution matched the RAW head, so those ids resolved transport
    keys and no host -- and on restricted Compose an agent with no default hosts
    (mini-swe) then got an empty allowlist and silently could not reach the model
    API. Assert over the whole alias map so a newly added alias cannot regress.
    """
    from oddish.config import _MODEL_PROVIDER_ALIASES
    from oddish.workers.harbor import model_hosts
    from oddish.workers.harbor.restricted_network import (
        _model_transport_base_url_keys,
    )

    offenders = []
    for alias in sorted(_MODEL_PROVIDER_ALIASES):
        model = f"{alias}/some-model"
        keys = _model_transport_base_url_keys(model)
        hosts = model_hosts.outbound_hosts_for_model(model, infer_bare_provider=True)
        if keys and not hosts:
            offenders.append(alias)
    assert not offenders, f"aliases resolve transport keys but no host: {offenders}"

    # Canonical spellings are unchanged by the alias canonicalization.
    assert model_hosts.outbound_hosts_for_model(
        "anthropic/claude", infer_bare_provider=True
    ) == ["api.anthropic.com", "mcp-proxy.anthropic.com"]
    # The public / single-container path does not canonicalize, so its
    # allowlists are untouched.
    assert model_hosts.outbound_hosts_for_model("claude/opus") == []


def test_transport_authoritative_agents_keep_their_model_identity():
    """Pinned transport and pinned model identity must travel together.

    gemini-cli and grok-build pin egress (infer_model=False) exactly as Cursor
    does, so the worker-private Azure deployment id must not be swapped onto
    their model -- the sandbox would carry an identity its pinned transport
    cannot resolve. The gate runs inside _build_agent_config, BEFORE the Oddish
    wrapper is applied, so the stock classes must report this too.
    """
    from oddish.workers.harbor.restricted_network import (
        agent_keeps_public_model_identity,
    )

    for name in ("cursor-cli", "gemini-cli", "grok-build"):
        config = AgentConfig(name=name, model_name="openai/gpt-4o")
        assert agent_keeps_public_model_identity(config), name
    for name in ("codex", "mini-swe-agent", "claude-code"):
        config = AgentConfig(name=name, model_name="openai/gpt-4o")
        assert not agent_keeps_public_model_identity(config), name

    # The narrower property is unchanged: only Cursor's own service fronts
    # arbitrary models. gemini-cli/grok-build are pinned provider clients.
    assert agent_fronts_own_model_service(AgentConfig(name="cursor-cli")) is True
    assert agent_fronts_own_model_service(AgentConfig(name="gemini-cli")) is False
    assert agent_fronts_own_model_service(AgentConfig(name="grok-build")) is False


def test_stock_harnesses_are_identity_only_and_still_fail_closed():
    """Registering the stock classes must not attest them.

    The Oddish wrapper is what disables provider-side web tools, so a trial
    pinned to the stock class by import_path (which the wrapper skips) must keep
    failing closed -- the registry entry exists only so the pre-wrapper
    deployment-swap gate reads the right transport identity.
    """
    from oddish.workers.harbor.restricted_network import (
        agent_keeps_public_model_identity,
    )

    for import_path in (
        "harbor.agents.installed.gemini_cli:GeminiCli",
        "harbor.agents.installed.grok_build:GrokBuild",
        "harbor.agents.installed.cursor_cli:CursorCli",
    ):
        config = AgentConfig(import_path=import_path, model_name="gemini/pro")
        assert agent_keeps_public_model_identity(config)
        with pytest.raises(RestrictedNetworkProfileError):
            restricted_network_profile_for_config(config, resolved_env={})

    # The wrappers themselves still resolve their real pinned profiles.
    gemini = restricted_network_profile_for_config(
        AgentConfig(
            import_path="oddish.workers.agents.gemini_cli:OddishGeminiCli",
            model_name="gemini/pro",
        ),
        resolved_env={},
    )
    assert gemini.outbound_hosts == ("generativelanguage.googleapis.com",)
    cursor = restricted_network_profile_for_config(
        AgentConfig(
            import_path="oddish.workers.agents.cursor_cli:OddishCursorCli",
            model_name="cursor/composer",
        ),
        resolved_env={},
    )
    assert cursor.outbound_hosts == ("*.cursor.sh",)
    assert cursor.kwarg_overrides == {"disable_web_tools": True}
    grok = restricted_network_profile_for_config(
        AgentConfig(
            import_path="oddish.workers.agents.grok_build:OddishGrokBuild",
            model_name="xai/grok-4",
        ),
        resolved_env={},
    )
    assert grok.outbound_hosts == ("api.x.ai",)
