from __future__ import annotations

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

    assert "x-session-id: 'swe-marathon--" in config_command
    assert "--model=openai/llama-eval-model" in run_command
    assert "-c mini -c /tmp/oddish-meta-mini-swe-agent.yaml" in run_command
    assert "reasoning_effort" not in run_command
    assert "temperature" not in run_command
    assert run_env is not None
    assert run_env["MSWEA_API_KEY"] == "meta-test-key"
    assert run_env["OPENAI_BASE_URL"] == "https://api.ai.meta.com/v1"
    assert run_env["OPENAI_API_BASE"] == "https://api.ai.meta.com/v1"
