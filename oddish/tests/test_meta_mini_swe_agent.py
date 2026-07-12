from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from oddish.workers.agents.mini_swe_agent import OddishMetaMiniSweAgent


@pytest.mark.asyncio
async def test_meta_mini_swe_agent_adds_session_header_and_openai_model(tmp_path):
    commands: list[tuple[str, dict | None]] = []

    class _FakeEnvironment:
        async def exec(self, command, user=None, env=None, cwd=None, timeout_sec=None):
            commands.append((command, env))
            return SimpleNamespace(return_code=0, stdout="", stderr="")

    agent = OddishMetaMiniSweAgent(
        logs_dir=tmp_path,
        model_name="meta/llama-eval-model",
        extra_env={
            "MSWEA_API_KEY": "meta-test-key",
            "OPENAI_BASE_URL": "https://api.ai.meta.com/v1",
            "ODDISH_META_EVAL_NAME": "SWE Marathon",
        },
    )

    await agent.run("fix the task", _FakeEnvironment(), SimpleNamespace())

    config_command = commands[-2][0]
    run_command, run_env = commands[-1]

    assert 'x-session-id: "swe-marathon--' in config_command
    assert "--model=openai/llama-eval-model" in run_command
    assert "-c mini -c /tmp/oddish-meta-mini-swe-agent.yaml" in run_command
    assert "reasoning_effort" not in run_command
    assert "temperature" not in run_command
    assert run_env is not None
    assert run_env["MSWEA_API_KEY"] == "meta-test-key"
    assert run_env["OPENAI_BASE_URL"] == "https://api.ai.meta.com/v1"
    assert run_env["OPENAI_API_BASE"] == "https://api.ai.meta.com/v1"


@pytest.mark.asyncio
async def test_meta_mini_swe_agent_json_quotes_explicit_session_id(tmp_path):
    commands: list[tuple[str, dict | None]] = []
    session_id = "swe-marathon--run'\\\nnext"

    class _FakeEnvironment:
        async def exec(self, command, user=None, env=None, cwd=None, timeout_sec=None):
            commands.append((command, env))
            return SimpleNamespace(return_code=0, stdout="", stderr="")

    agent = OddishMetaMiniSweAgent(
        logs_dir=tmp_path,
        model_name="meta/llama-eval-model",
        extra_env={
            "MSWEA_API_KEY": "meta-test-key",
            "ODDISH_META_SESSION_ID": session_id,
        },
    )

    await agent.run("fix the task", _FakeEnvironment(), SimpleNamespace())

    assert f"x-session-id: {json.dumps(session_id)}" in commands[-2][0]


@pytest.mark.asyncio
async def test_meta_mini_swe_agent_forwards_reasoning_effort(tmp_path):
    commands: list[tuple[str, dict | None]] = []

    class _FakeEnvironment:
        async def exec(self, command, user=None, env=None, cwd=None, timeout_sec=None):
            commands.append((command, env))
            return SimpleNamespace(return_code=0, stdout="", stderr="")

    agent = OddishMetaMiniSweAgent(
        logs_dir=tmp_path,
        model_name="meta/llama-eval-model",
        reasoning_effort="xhigh",
        extra_env={
            "MSWEA_API_KEY": "meta-test-key",
            "OPENAI_BASE_URL": "https://api.ai.meta.com/v1",
        },
    )

    await agent.run("fix the task", _FakeEnvironment(), SimpleNamespace())

    run_command = commands[-1][0]
    # meta/ models are not openai/-prefixed, so effort rides in extra_body of the
    # chat-completions request body.
    assert (
        "model.model_kwargs.extra_body.reasoning_effort=xhigh" in run_command
    )


@pytest.mark.asyncio
async def test_meta_mini_swe_agent_delivers_task_via_config_not_argv(tmp_path):
    commands: list[tuple[str, dict | None]] = []

    class _FakeEnvironment:
        async def exec(self, command, user=None, env=None, cwd=None, timeout_sec=None):
            commands.append((command, env))
            return SimpleNamespace(return_code=0, stdout="", stderr="")

    marker = "ZZ-restart-the-vinext-dev-server-ZZ"
    agent = OddishMetaMiniSweAgent(
        logs_dir=tmp_path,
        model_name="meta/llama-eval-model",
        extra_env={"MSWEA_API_KEY": "meta-test-key"},
    )

    await agent.run(marker, _FakeEnvironment(), SimpleNamespace())

    config_command = commands[-2][0]
    run_command = commands[-1][0]
    # Task rides in the config file (run.task), not on argv, so the agent's own
    # `pkill -f <keyword>` cannot match the mini-swe-agent process cmdline.
    assert "run:" in config_command and "task:" in config_command
    assert marker in config_command
    assert "--task=" not in run_command
    assert marker not in run_command


def test_meta_mini_swe_agent_allowlists_custom_base_url(monkeypatch):
    monkeypatch.setattr(
        "oddish.workers.agents.mini_swe_agent.settings.meta_base_url",
        "https://settings.meta.example/v1",
    )

    domains = OddishMetaMiniSweAgent.required_outbound_domains(
        kwargs={
            "extra_env": {
                "OPENAI_BASE_URL": "https://relay.meta.example/v1",
                "OPENAI_API_BASE": "https://api-base.meta.example/v1",
            }
        }
    )

    assert "api.ai.meta.com" in domains
    assert "settings.meta.example" in domains
    assert "relay.meta.example" in domains
    assert "api-base.meta.example" in domains
