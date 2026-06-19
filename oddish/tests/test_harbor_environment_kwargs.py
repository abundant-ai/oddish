from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harbor.models.environment_type import EnvironmentType

from oddish.cli import api as cli_api
from oddish.core.sweeps import (
    build_task_submission_from_sweep,
    build_trial_specs_from_sweep,
)
from oddish.queue import _build_harbor_config_for_trial
from oddish.schemas import TaskSweepSubmission
from oddish.workers import harbor_runner


AGENT_TOOLS_IMAGE = "ghcr.io/org/harbor-agent-tools:tag"


class _SubmittingClient:
    def __init__(self, payloads: list[dict]):
        self.payloads = payloads

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def post(
        self, _url: str, *, json: dict, headers: dict | None = None
    ) -> httpx.Response:
        self.payloads.append(json)
        return httpx.Response(200, json={"task_id": "task-123"})


def test_submit_sweep_includes_harbor_environment_kwargs(monkeypatch) -> None:
    payloads: list[dict] = []

    def fake_client(*_args: object, **_kwargs: object) -> _SubmittingClient:
        return _SubmittingClient(payloads)

    monkeypatch.setattr(cli_api.httpx, "Client", fake_client)
    monkeypatch.setattr(cli_api, "get_auth_headers", lambda: {})

    cli_api.submit_sweep(
        api_url="https://api.example",
        task_id="task-123",
        configs=[{"agent": "codex", "model": "gpt-5", "n_trials": 1}],
        environment=EnvironmentType.MODAL,
        user=None,
        priority="low",
        experiment_id=None,
        harbor_config={
            "environment": {
                "kwargs": {
                    "agent_tools_image": "ghcr.io/org/old-tools:tag",
                    "keep": "value",
                }
            }
        },
        environment_kwargs=[
            f"agent_tools_image={AGENT_TOOLS_IMAGE}",
            "extra=value",
        ],
        override_cpus=4,
    )

    assert payloads[0]["harbor"]["environment"] == {
        "kwargs": {
            "agent_tools_image": AGENT_TOOLS_IMAGE,
            "keep": "value",
            "extra": "value",
        },
        "override_cpus": 4,
    }


def test_sweep_config_loader_preserves_raw_harbor_block(tmp_path: Path) -> None:
    config_path = tmp_path / "sweep.yaml"
    config_path.write_text(
        f"""
agents:
  - name: codex
    model_name: gpt-5
harbor:
  environment:
    kwargs:
      agent_tools_image: {AGENT_TOOLS_IMAGE}
""".strip()
    )

    config = cli_api.load_sweep_config(config_path)

    assert config["harbor"]["environment"]["kwargs"]["agent_tools_image"] == (
        AGENT_TOOLS_IMAGE
    )


def test_sweep_config_loader_allows_oracle_without_model_name(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "sweep.yaml"
    config_path.write_text(
        f"""
agents:
  - name: oracle
    n_trials: 1
harbor:
  environment:
    kwargs:
      agent_tools_image: {AGENT_TOOLS_IMAGE}
""".strip()
    )

    config = cli_api.load_sweep_config(config_path)

    assert config["agents"] == [
        {
            "agent": "oracle",
            "model": None,
            "n_trials": 1,
        }
    ]
    assert config["harbor"]["environment"]["kwargs"]["agent_tools_image"] == (
        AGENT_TOOLS_IMAGE
    )


def test_harbor_environment_kwargs_survive_trial_config_round_trip() -> None:
    submission = TaskSweepSubmission(
        task_id="task-123",
        configs=[{"agent": "codex", "model": "gpt-5"}],
        harbor={
            "environment": {
                "kwargs": {
                    "agent_tools_image": AGENT_TOOLS_IMAGE,
                }
            }
        },
    )

    trials = build_trial_specs_from_sweep(submission)
    task_submission = build_task_submission_from_sweep(
        submission,
        task_path="/tmp/task",
        trials=trials,
    )
    harbor_config = _build_harbor_config_for_trial(task_submission, trials[0])

    assert harbor_config is not None
    assert harbor_config["environment"]["kwargs"]["agent_tools_image"] == (
        AGENT_TOOLS_IMAGE
    )


def test_claude_code_openrouter_agent_config_sets_anthropic_skin_env(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)

    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model="openrouter/anthropic/claude-opus-4.8",
        raw_harbor_config={},
    )

    assert agent_config.model_name == "openrouter/anthropic/claude-opus-4.8"
    assert agent_config.env["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"
    assert agent_config.env["ANTHROPIC_AUTH_TOKEN"] == "${OPENROUTER_API_KEY}"
    assert agent_config.env["ENABLE_TOOL_SEARCH"] == "false"
    assert agent_config.env["ANTHROPIC_API_KEY"] == ""
    assert agent_config.env["CLAUDE_CODE_USE_BEDROCK"] == ""
    assert agent_config.env["AWS_BEARER_TOKEN_BEDROCK"] == ""


def test_claude_code_openrouter_agent_config_preserves_explicit_base_and_token(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.example/api")

    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model="openrouter/anthropic/claude-opus-4.8",
        raw_harbor_config={
            "agent_config": {
                "env": {
                    "ANTHROPIC_BASE_URL": "https://custom.example/api",
                    "ANTHROPIC_AUTH_TOKEN": "${CUSTOM_OPENROUTER_TOKEN}",
                }
            }
        },
    )

    assert agent_config.env["ANTHROPIC_BASE_URL"] == "https://custom.example/api"
    assert agent_config.env["ANTHROPIC_AUTH_TOKEN"] == "${CUSTOM_OPENROUTER_TOKEN}"
    assert agent_config.env["ANTHROPIC_API_KEY"] == ""


def test_non_openrouter_claude_code_agent_config_does_not_add_openrouter_env(
    monkeypatch,
) -> None:
    # This test is about openrouter env injection, not Bedrock vs direct routing;
    # pin the force-direct mitigation off and the Bedrock env on so the incidental
    # model-id assertion exercises the Bedrock branch deterministically.
    monkeypatch.setattr(harbor_runner.settings, "claude_code_force_direct_api", False)
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model="claude-opus-4-8",
        raw_harbor_config={},
    )

    assert agent_config.model_name == "global.anthropic.claude-opus-4-8"
    assert "ANTHROPIC_AUTH_TOKEN" not in agent_config.env
    assert "ANTHROPIC_BASE_URL" not in agent_config.env


def test_claude_code_glm_agent_config_sets_zai_anthropic_skin_env(
    monkeypatch,
) -> None:
    monkeypatch.delenv("ZAI_BASE_URL", raising=False)

    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model="zai/glm-x-preview[1m]",
        raw_harbor_config={},
    )

    # The "zai/" prefix stays on model_name so Harbor's per-agent network
    # allowlist resolves api.z.ai for closed-internet tasks.
    assert agent_config.model_name == "zai/glm-x-preview[1m]"
    assert agent_config.env["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"
    assert agent_config.env["ANTHROPIC_AUTH_TOKEN"] == "${ZAI_API_KEY}"
    # The bare GLM id (no prefix) is what Claude Code must send, mirrored across
    # every size alias since the image defaults to Bedrock mode.
    assert agent_config.env["ANTHROPIC_MODEL"] == "glm-x-preview[1m]"
    assert agent_config.env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "glm-x-preview[1m]"
    assert agent_config.env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "glm-x-preview[1m]"
    assert agent_config.env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "glm-x-preview[1m]"
    # Ambient Bedrock/Anthropic creds blanked so the z.ai route wins.
    assert agent_config.env["ANTHROPIC_API_KEY"] == ""
    assert agent_config.env["CLAUDE_CODE_USE_BEDROCK"] == ""
    assert agent_config.env["AWS_BEARER_TOKEN_BEDROCK"] == ""
    # z.ai's recommended "max effort" + adaptive thinking, rendered by Harbor's
    # claude-code agent as `--effort max --thinking adaptive`.
    assert agent_config.kwargs["thinking"] == "adaptive"
    assert agent_config.kwargs["reasoning_effort"] == "max"


def test_claude_code_glm_recommended_kwargs_render_as_cli_flags() -> None:
    """The kwargs Oddish sets for GLM produce z.ai's recommended CLI flags."""
    import tempfile

    from harbor.agents.installed.claude_code import ClaudeCode

    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model="zai/glm-x-preview[1m]",
        raw_harbor_config={},
    )
    agent = ClaudeCode(
        logs_dir=Path(tempfile.mkdtemp()),
        model_name=agent_config.model_name,
        **agent_config.kwargs,
    )
    flags = agent.build_cli_flags()
    assert "--effort max" in flags
    assert "--thinking adaptive" in flags


def test_claude_code_glm_kwargs_are_overridable() -> None:
    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model="zai/glm-x-preview[1m]",
        raw_harbor_config={"agent_config": {"kwargs": {"reasoning_effort": "high"}}},
    )

    assert agent_config.kwargs["reasoning_effort"] == "high"
    assert agent_config.kwargs["thinking"] == "adaptive"


def test_claude_code_bare_glm_model_is_canonicalized_to_zai(monkeypatch) -> None:
    monkeypatch.delenv("ZAI_BASE_URL", raising=False)

    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model="glm-x-preview[1m]",
        raw_harbor_config={},
    )

    assert agent_config.model_name == "zai/glm-x-preview[1m]"
    assert agent_config.env["ANTHROPIC_MODEL"] == "glm-x-preview[1m]"


def test_claude_code_minimax_agent_config_sets_minimax_skin_env(monkeypatch) -> None:
    monkeypatch.delenv("MINIMAX_BASE_URL", raising=False)

    # Trials store the canonical (lowercased) id; _build_agent_config receives
    # that, not the raw mixed-case input.
    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model="minimax/minimax-m3",
        raw_harbor_config={},
    )

    # The provider prefix stays so Harbor's allowlist resolves api.minimax.io
    # for closed-internet tasks.
    assert agent_config.model_name == "minimax/minimax-m3"
    assert agent_config.env["ANTHROPIC_BASE_URL"] == "https://api.minimax.io/anthropic"
    assert agent_config.env["ANTHROPIC_AUTH_TOKEN"] == "${MINIMAX_API_KEY}"
    # The endpoint expects the exact mixed-case id, mirrored across aliases.
    assert agent_config.env["ANTHROPIC_MODEL"] == "MiniMax-M3"
    assert agent_config.env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "MiniMax-M3"
    assert agent_config.env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "MiniMax-M3"
    assert agent_config.env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "MiniMax-M3"
    assert agent_config.env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "512000"
    # Ambient Bedrock/Anthropic creds blanked so the MiniMax route wins.
    assert agent_config.env["ANTHROPIC_API_KEY"] == ""
    assert agent_config.env["CLAUDE_CODE_USE_BEDROCK"] == ""
    assert agent_config.env["AWS_BEARER_TOKEN_BEDROCK"] == ""
    # No thinking/effort kwargs (MiniMax M3 has thinking on by default).
    assert "thinking" not in agent_config.kwargs
    assert "reasoning_effort" not in agent_config.kwargs


def test_claude_code_moonshot_agent_config_sets_moonshot_skin_env(monkeypatch) -> None:
    monkeypatch.delenv("MOONSHOT_BASE_URL", raising=False)

    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model="kimi-k2.7-code",
        raw_harbor_config={},
    )

    assert agent_config.model_name == "moonshot/kimi-k2.7-code"
    assert agent_config.env["ANTHROPIC_BASE_URL"] == "https://api.moonshot.ai/anthropic"
    assert agent_config.env["ANTHROPIC_AUTH_TOKEN"] == "${MOONSHOT_API_KEY}"
    assert agent_config.env["ANTHROPIC_MODEL"] == "kimi-k2.7-code"
    assert agent_config.env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "kimi-k2.7-code"
    assert agent_config.env["CLAUDE_CODE_SUBAGENT_MODEL"] == "kimi-k2.7-code"
    assert agent_config.env["ENABLE_TOOL_SEARCH"] == "false"
    assert agent_config.env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "262144"
    assert agent_config.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "32768"
    assert agent_config.env["ANTHROPIC_API_KEY"] == ""
    assert agent_config.env["CLAUDE_CODE_USE_BEDROCK"] == ""
    assert agent_config.env["AWS_BEARER_TOKEN_BEDROCK"] == ""
    # K2.7 locks sampling params / thinking is always on -- no kwargs set.
    assert "thinking" not in agent_config.kwargs
    assert "reasoning_effort" not in agent_config.kwargs


def test_claude_code_fireworks_agent_config_sets_fireworks_skin_env(
    monkeypatch,
) -> None:
    monkeypatch.delenv("FIREWORKS_BASE_URL", raising=False)

    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model="fireworks/glm-5.2",
        raw_harbor_config={},
    )

    # Friendly alias collapses to the canonical short id; the ``fireworks/``
    # prefix stays on model_name so Harbor's allowlist can resolve the Fireworks
    # endpoint for closed-internet tasks (once the fork maps it).
    assert agent_config.model_name == "fireworks/glm-5p2"
    assert agent_config.env["ANTHROPIC_BASE_URL"] == "https://api.fireworks.ai/inference"
    assert agent_config.env["ANTHROPIC_AUTH_TOKEN"] == "${FIREWORKS_API_KEY}"
    # Claude Code must send the full Fireworks model path, mirrored across every
    # size alias since the image defaults to Bedrock mode.
    expected = "accounts/fireworks/models/glm-5p2"
    assert agent_config.env["ANTHROPIC_MODEL"] == expected
    assert agent_config.env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == expected
    assert agent_config.env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == expected
    assert agent_config.env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == expected
    assert agent_config.env["CLAUDE_CODE_SUBAGENT_MODEL"] == expected
    assert agent_config.env["ENABLE_TOOL_SEARCH"] == "false"
    # Ambient Bedrock/Anthropic creds blanked so the Fireworks route wins.
    assert agent_config.env["ANTHROPIC_API_KEY"] == ""
    assert agent_config.env["CLAUDE_CODE_USE_BEDROCK"] == ""
    assert agent_config.env["AWS_BEARER_TOKEN_BEDROCK"] == ""
    # Default claude-code agent -- no forced thinking/effort (also required since
    # Fireworks rejects thinking params on some hosted models, e.g. Kimi).
    assert "thinking" not in agent_config.kwargs
    assert "reasoning_effort" not in agent_config.kwargs


def test_claude_code_fireworks_kimi_and_minimax_full_paths(monkeypatch) -> None:
    monkeypatch.delenv("FIREWORKS_BASE_URL", raising=False)

    for raw, expected_model in (
        ("fireworks/kimi-k2.7-code", "accounts/fireworks/models/kimi-k2p7-code"),
        ("fireworks/minimax-m3", "accounts/fireworks/models/minimax-m3"),
    ):
        agent_config = harbor_runner._build_agent_config(
            agent="claude-code",
            model=raw,
            raw_harbor_config={},
        )
        assert agent_config.env["ANTHROPIC_MODEL"] == expected_model, raw
        assert agent_config.env["ANTHROPIC_AUTH_TOKEN"] == "${FIREWORKS_API_KEY}", raw


def test_claude_code_fireworks_does_not_trigger_zai_route(monkeypatch) -> None:
    monkeypatch.delenv("FIREWORKS_BASE_URL", raising=False)
    monkeypatch.delenv("ZAI_BASE_URL", raising=False)

    # ``fireworks/glm-...`` must hit Fireworks, not z.ai -- the GLM id must not
    # leak the trial back onto the z.ai base URL / token.
    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model="fireworks/glm-5.2",
        raw_harbor_config={},
    )

    assert "fireworks" in agent_config.env["ANTHROPIC_BASE_URL"]
    assert agent_config.env["ANTHROPIC_AUTH_TOKEN"] == "${FIREWORKS_API_KEY}"


def test_claude_code_fireworks_agent_config_preserves_explicit_env(monkeypatch) -> None:
    monkeypatch.delenv("FIREWORKS_BASE_URL", raising=False)

    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model="fireworks/glm-5.2",
        raw_harbor_config={
            "agent_config": {
                "env": {
                    "ANTHROPIC_BASE_URL": "https://custom.example/anthropic",
                    "ANTHROPIC_AUTH_TOKEN": "${CUSTOM_FW_TOKEN}",
                }
            }
        },
    )

    assert agent_config.env["ANTHROPIC_BASE_URL"] == "https://custom.example/anthropic"
    assert agent_config.env["ANTHROPIC_AUTH_TOKEN"] == "${CUSTOM_FW_TOKEN}"
    assert agent_config.env["ANTHROPIC_API_KEY"] == ""


def test_claude_code_openrouter_kimi_is_not_routed_to_moonshot(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")

    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model="openrouter/moonshotai/kimi-k2.7-code",
        raw_harbor_config={},
    )

    # The OpenRouter route must win: model id unchanged, OpenRouter base URL /
    # token, not the Moonshot-direct endpoint or its recommended env.
    assert agent_config.model_name == "openrouter/moonshotai/kimi-k2.7-code"
    assert "openrouter" in agent_config.env["ANTHROPIC_BASE_URL"]
    assert agent_config.env["ANTHROPIC_AUTH_TOKEN"] == "${OPENROUTER_API_KEY}"
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in agent_config.env
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in agent_config.env


def test_claude_code_glm_agent_config_preserves_explicit_env(monkeypatch) -> None:
    monkeypatch.delenv("ZAI_BASE_URL", raising=False)

    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model="zai/glm-x-preview[1m]",
        raw_harbor_config={
            "agent_config": {
                "env": {
                    "ANTHROPIC_BASE_URL": "https://custom.example/anthropic",
                    "ANTHROPIC_AUTH_TOKEN": "${CUSTOM_ZAI_TOKEN}",
                    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000",
                }
            }
        },
    )

    assert agent_config.env["ANTHROPIC_BASE_URL"] == "https://custom.example/anthropic"
    assert agent_config.env["ANTHROPIC_AUTH_TOKEN"] == "${CUSTOM_ZAI_TOKEN}"
    assert agent_config.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "64000"
    assert agent_config.env["ANTHROPIC_API_KEY"] == ""


def test_harbor_runner_passes_environment_kwargs_to_job_config(
    monkeypatch,
    tmp_path: Path,
) -> None:
    task_path = tmp_path / "task"
    task_path.mkdir()
    jobs_dir = tmp_path / "jobs"
    seen: dict[str, dict] = {}

    class _FakeJob:
        def __init__(self, config: dict):
            self.job_dir = config["jobs_dir"] / "job-1"
            seen["environment_kwargs"] = config["environment"].kwargs

        @classmethod
        async def create(cls, config: dict):
            return cls(config)

        async def run(self):
            self.job_dir.mkdir(parents=True, exist_ok=True)
            (self.job_dir / "result.json").write_text("{}\n", encoding="utf-8")
            return object()

    monkeypatch.setattr(
        harbor_runner, "validate_task_timeout_config", lambda path: None
    )
    monkeypatch.setattr(harbor_runner, "_build_agent_config", lambda **kwargs: object())
    monkeypatch.setattr(harbor_runner, "TaskConfig", lambda path: path)
    monkeypatch.setattr(harbor_runner, "JobConfig", lambda **kwargs: kwargs)
    monkeypatch.setattr(harbor_runner, "Job", _FakeJob)
    monkeypatch.setattr(
        harbor_runner,
        "_extract_outcome_from_job_result",
        lambda **kwargs: harbor_runner.HarborOutcome(
            reward=1.0,
            error=None,
            exit_code=0,
            duration_sec=kwargs["duration_sec"],
            job_result_path=kwargs["job_result_path"],
            job_dir=kwargs["job_dir"],
        ),
    )

    outcome = asyncio.run(
        harbor_runner.run_harbor_trial_async(
            task_path=task_path,
            agent="nop",
            jobs_dir=jobs_dir,
            environment=EnvironmentType.MODAL,
            harbor_config={
                "environment": {
                    "kwargs": {
                        "agent_tools_image": AGENT_TOOLS_IMAGE,
                    }
                }
            },
        )
    )

    assert outcome.error is None
    assert seen["environment_kwargs"]["agent_tools_image"] == AGENT_TOOLS_IMAGE
