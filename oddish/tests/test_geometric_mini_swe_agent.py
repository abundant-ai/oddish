from __future__ import annotations

from types import SimpleNamespace

import pytest

from oddish.config import GEOMETRIC_DEFAULT_BASE_URL
from oddish.workers.agents.mini_swe_agent import OddishGeometricMiniSweAgent
from oddish.workers.agents.network import normalize_domain_or_url


class _FakeEnvironment:
    def __init__(self, commands: list[tuple[str, dict | None]]) -> None:
        self._commands = commands

    async def exec(self, command, user=None, env=None, cwd=None, timeout_sec=None):
        self._commands.append((command, env))
        return SimpleNamespace(return_code=0, stdout="", stderr="")


@pytest.mark.asyncio
async def test_geometric_mini_swe_agent_uses_openai_model_and_own_config(tmp_path):
    commands: list[tuple[str, dict | None]] = []

    agent = OddishGeometricMiniSweAgent(
        logs_dir=tmp_path,
        model_name="geometric/glm-5.3",
        extra_env={
            "MSWEA_API_KEY": "geometric-test-key",
            "OPENAI_BASE_URL": "https://api.geometric.example/v1",
        },
    )

    await agent.run("fix the task", _FakeEnvironment(commands), SimpleNamespace())

    config_command = commands[-2][0]
    run_command, run_env = commands[-1]

    # litellm reaches Geometric through its openai/ provider, so the harness
    # must be handed the bare id under that prefix -- not ``geometric/``.
    assert "--model=openai/glm-5.3" in run_command
    assert "-c mini -c /tmp/oddish-geometric-mini-swe-agent.yaml" in run_command
    # Its own config path, so a Geometric trial cannot collide with the Meta or
    # stock mini-swe config in a shared sandbox.
    assert "/tmp/oddish-meta-mini-swe-agent.yaml" not in run_command
    assert "_skip_mcp_handler: true" in config_command
    # No vendor session header: that is Meta-specific.
    assert "x-session-id" not in config_command
    assert run_env is not None
    assert run_env["MSWEA_API_KEY"] == "geometric-test-key"
    assert run_env["OPENAI_BASE_URL"] == "https://api.geometric.example/v1"


@pytest.mark.asyncio
async def test_geometric_mini_swe_agent_gm_alias_resolves_same_bare_id(tmp_path):
    commands: list[tuple[str, dict | None]] = []

    agent = OddishGeometricMiniSweAgent(
        logs_dir=tmp_path,
        model_name="gm/glm-5.3",
        extra_env={"MSWEA_API_KEY": "geometric-test-key"},
    )

    await agent.run("fix the task", _FakeEnvironment(commands), SimpleNamespace())

    assert "--model=openai/glm-5.3" in commands[-1][0]


@pytest.mark.asyncio
async def test_geometric_mini_swe_agent_delivers_task_via_config_not_argv(tmp_path):
    commands: list[tuple[str, dict | None]] = []

    marker = "ZZ-restart-the-vinext-dev-server-ZZ"
    agent = OddishGeometricMiniSweAgent(
        logs_dir=tmp_path,
        model_name="geometric/glm-5.3",
        extra_env={"MSWEA_API_KEY": "geometric-test-key"},
    )

    await agent.run(marker, _FakeEnvironment(commands), SimpleNamespace())

    config_command = commands[-2][0]
    run_command = commands[-1][0]
    # Task rides in the config file (run.task), not on argv, so the agent's own
    # `pkill -f <keyword>` cannot match the mini-swe-agent process cmdline.
    assert "run:" in config_command and "task:" in config_command
    assert marker in config_command
    assert "--task=" not in run_command
    assert marker not in run_command


@pytest.mark.asyncio
async def test_geometric_mini_swe_agent_forwards_reasoning_effort(tmp_path):
    commands: list[tuple[str, dict | None]] = []

    agent = OddishGeometricMiniSweAgent(
        logs_dir=tmp_path,
        model_name="geometric/glm-5.3",
        reasoning_effort="xhigh",
        extra_env={"MSWEA_API_KEY": "geometric-test-key"},
    )

    await agent.run("fix the task", _FakeEnvironment(commands), SimpleNamespace())

    assert "model.model_kwargs.reasoning_effort=xhigh" in commands[-1][0]


def test_geometric_required_outbound_domains_honors_extra_env_override():
    domains = OddishGeometricMiniSweAgent.required_outbound_domains(
        model_name="geometric/glm-5.3",
        kwargs={"extra_env": {"GEOMETRIC_BASE_URL": "https://relay.example:8443/v1"}},
    )

    assert "relay.example" in domains


def test_geometric_required_outbound_domains_includes_configured_default():
    domains = OddishGeometricMiniSweAgent.required_outbound_domains(
        model_name="geometric/glm-5.3"
    )

    # The configured endpoint must be reachable even with no override, or a
    # restricted-network trial has no route to the model API at all.
    assert domains
    assert normalize_domain_or_url(GEOMETRIC_DEFAULT_BASE_URL) in domains


def test_geometric_agent_refuses_a_model_the_endpoint_does_not_serve():
    # Defense in depth: submit already rejects these, but the harness must not
    # hand litellm an ``openai/<foreign-model>`` id even if one slipped through.
    with pytest.raises(ValueError):
        OddishGeometricMiniSweAgent._oddish_bare_model_id("geometric/gpt-4o")

    assert (
        OddishGeometricMiniSweAgent._oddish_bare_model_id("geometric/glm-5.3")
        == "glm-5.3"
    )
