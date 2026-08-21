import json
from pathlib import Path

import pytest
from harbor.llms.base import LLMResponse
from harbor.models.agent.context import AgentContext
from harbor.models.metric import UsageInfo

from oddish.analyze.analysis_activity import build_analysis_activity_summary
from oddish.workers.harbor import single_llm_agent
from oddish.workers.harbor.agent_config import _build_agent_config


class RecordingEnvironment:
    def __init__(self, *, upload_error: Exception | None = None) -> None:
        self.upload_error = upload_error
        self.uploads: list[tuple[Path, str, str]] = []

    async def upload_file(self, source_path, target_path):
        source = Path(source_path)
        self.uploads.append((source, target_path, source.read_text()))
        if self.upload_error is not None:
            raise self.upload_error


@pytest.mark.asyncio
async def test_single_llm_agent_writes_artifact_trajectory_and_context(
    monkeypatch, tmp_path
):
    calls = []

    class FakeLLM:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        async def call(self, prompt, **kwargs):
            calls.append((prompt, kwargs))
            return LLMResponse(
                content=json.dumps(
                    {
                        "target_trial_id": "t-42",
                        "trajectory_summary": {
                            "summary": "The agent finished the task.",
                            "highlights": [],
                            "components": [
                                {
                                    "step_ids": [1],
                                    "trajectory_component": "implementing",
                                    "action": "edit",
                                    "purpose": "build",
                                    "summary": "The agent changed the solution.",
                                }
                            ],
                        },
                    }
                ),
                reasoning_content="one thought",
                model_name="anthropic/test-model",
                usage=UsageInfo(
                    prompt_tokens=12,
                    completion_tokens=4,
                    cache_tokens=2,
                    cost_usd=0.03,
                ),
            )

    monkeypatch.setattr(single_llm_agent, "LiteLLM", FakeLLM)
    agent = single_llm_agent.SingleLLMAgent(
        logs_dir=tmp_path,
        model_name="anthropic/test-model",
        output_filename="result.json",
        response_model_import_path=(
            "oddish.analyze.trajectory_summary_models:SummarizeResultOutput"
        ),
        extra_env={"ANTHROPIC_API_KEY": "scoped-key"},
    )
    context = AgentContext()
    environment = RecordingEnvironment()

    await agent.run("Return JSON.", environment, context)

    assert calls[0] == (
        "init",
        {"model_name": "anthropic/test-model", "api_key": "scoped-key"},
    )
    assert calls[1][0] == "Return JSON."
    assert calls[1][1]["response_format"].__name__ == "SummarizeResultOutput"
    assert json.loads((tmp_path / "result.json").read_text())["target_trial_id"] == (
        "t-42"
    )
    assert len(environment.uploads) == 1
    uploaded_source, uploaded_target, uploaded_content = environment.uploads[0]
    assert uploaded_source == tmp_path / "result.json"
    assert uploaded_target == "/logs/result.json"
    assert json.loads(uploaded_content)["target_trial_id"] == "t-42"

    trajectory = json.loads((tmp_path / "trajectory.json").read_text())
    assert [step["source"] for step in trajectory["steps"]] == [
        "user",
        "agent",
        "agent",
    ]
    assert trajectory["steps"][1]["llm_call_count"] == 1
    assert trajectory["steps"][1]["metrics"]["cost_usd"] == 0.03
    assert trajectory["steps"][2]["llm_call_count"] == 0
    assert trajectory["steps"][2]["tool_calls"][0]["function_name"] == "Write"
    assert trajectory["steps"][2]["tool_calls"][0]["arguments"]["file_path"] == (
        uploaded_target
    )
    assert trajectory["final_metrics"]["total_prompt_tokens"] == 12
    activity = build_analysis_activity_summary(
        kind="summarize",
        task_name="demo",
        trial_count=1,
        status="success",
        artifact_name="result.json",
        trajectory=trajectory,
    )
    assert activity["components"][-1]["trajectory_component"] == "writing_result"
    assert context.n_input_tokens == 12
    assert context.n_output_tokens == 4
    assert context.n_cache_tokens == 2
    assert context.cost_usd == 0.03


@pytest.mark.asyncio
async def test_single_llm_agent_preserves_usage_when_artifact_upload_fails(
    monkeypatch, tmp_path
):
    class FakeLLM:
        def __init__(self, **kwargs):
            pass

        async def call(self, prompt, **kwargs):
            return LLMResponse(
                content=json.dumps(
                    {
                        "target_trial_id": "t-42",
                        "trajectory_summary": {
                            "summary": "The agent finished the task.",
                            "highlights": [],
                            "components": [
                                {
                                    "step_ids": [1],
                                    "trajectory_component": "implementing",
                                    "action": "edit",
                                    "purpose": "build",
                                    "summary": "The agent changed the solution.",
                                }
                            ],
                        },
                    }
                ),
                model_name="anthropic/test-model",
                usage=UsageInfo(
                    prompt_tokens=12,
                    completion_tokens=4,
                    cache_tokens=2,
                    cost_usd=0.03,
                ),
            )

    monkeypatch.setattr(single_llm_agent, "LiteLLM", FakeLLM)
    agent = single_llm_agent.SingleLLMAgent(
        logs_dir=tmp_path,
        model_name="anthropic/test-model",
        output_filename="result.json",
        response_model_import_path=(
            "oddish.analyze.trajectory_summary_models:SummarizeResultOutput"
        ),
    )
    context = AgentContext()
    environment = RecordingEnvironment(upload_error=RuntimeError("upload failed"))

    with pytest.raises(RuntimeError, match="upload failed"):
        await agent.run("Return JSON.", environment, context)

    assert environment.uploads[0][1] == "/logs/result.json"
    assert context.n_input_tokens == 12
    assert context.n_output_tokens == 4
    assert context.n_cache_tokens == 2
    assert context.cost_usd == 0.03


def test_single_llm_agent_rejects_an_output_path(tmp_path):
    with pytest.raises(ValueError, match="file name"):
        single_llm_agent.SingleLLMAgent(
            logs_dir=tmp_path,
            model_name="anthropic/test-model",
            output_filename="nested/result.json",
            response_model_import_path=(
                "oddish.analyze.trajectory_summary_models:SummarizeResultOutput"
            ),
        )


def test_oddish_preserves_the_custom_agent_and_builds_a_litellm_model_id():
    agent_config = _build_agent_config(
        agent="single-llm",
        model="global.anthropic.claude-sonnet-4-6",
        raw_harbor_config={
            "agent_config": {
                "import_path": (
                    "oddish.workers.harbor.single_llm_agent:SingleLLMAgent"
                ),
                "kwargs": {
                    "output_filename": "summary_result.json",
                    "response_model_import_path": (
                        "oddish.analyze.trajectory_summary_models:SummarizeResultOutput"
                    ),
                },
            }
        },
    )

    assert agent_config.name is None
    assert agent_config.import_path.endswith(":SingleLLMAgent")
    assert agent_config.model_name == "bedrock/global.anthropic.claude-sonnet-4-6"
