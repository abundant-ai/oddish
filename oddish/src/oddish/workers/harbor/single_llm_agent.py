"""Harbor agent for one structured LLM request with no tool loop."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, override

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.llms.lite_llm import LiteLLM
from harbor.models.agent.context import AgentContext
from harbor.models.trial.paths import EnvironmentPaths
from harbor.models.trajectories import (
    Agent,
    FinalMetrics,
    Metrics,
    Observation,
    ObservationResult,
    Step,
    ToolCall,
    Trajectory,
)
from harbor.utils.import_path import import_class
from harbor.utils.trajectory_utils import format_trajectory_json
from pydantic import BaseModel


class SingleLLMAgent(BaseAgent):
    """Turn one Harbor instruction into one validated JSON artifact."""

    SUPPORTS_ATIF = True

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        *,
        output_filename: str,
        response_model_import_path: str,
        max_tokens: int = 16_384,
        **kwargs: Any,
    ) -> None:
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        if Path(output_filename).name != output_filename:
            raise ValueError("output_filename must be a file name, not a path")
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        self._output_filename = output_filename
        self._response_model = import_class(
            response_model_import_path,
            base=BaseModel,
            label="single LLM response model",
        )
        self._max_tokens = max_tokens

    @staticmethod
    @override
    def name() -> str:
        return "single-llm"

    @override
    def version(self) -> str:
        return "1.0.0"

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        return

    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        if not self.model_name:
            raise ValueError("SingleLLMAgent requires a model_name")

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        started_at = datetime.now(timezone.utc).isoformat()
        llm_kwargs: dict[str, Any] = {}
        anthropic_key = self.extra_env.get("ANTHROPIC_API_KEY")
        if anthropic_key:
            # Job-scoped credentials live in AgentConfig.env. Installed agents
            # forward that mapping into their subprocess; this host-side agent
            # passes the same key directly to LiteLLM without mutating os.environ.
            llm_kwargs["api_key"] = anthropic_key
        llm = LiteLLM(model_name=self.model_name, **llm_kwargs)
        response = await llm.call(
            instruction,
            response_format=self._response_model,
            max_tokens=self._max_tokens,
            logging_path=self.logs_dir / "llm_call.json",
        )
        finished_at = datetime.now(timezone.utc).isoformat()

        usage = response.usage
        if usage is not None:
            # The provider has already billed this request. Preserve its usage
            # even when response validation or artifact publication fails.
            context.n_input_tokens = usage.prompt_tokens
            context.n_output_tokens = usage.completion_tokens
            context.n_cache_tokens = usage.cache_tokens
            context.cost_usd = usage.cost_usd

        artifact = self._response_model.model_validate_json(
            response.content
        ).model_dump(mode="json")
        local_artifact_path = self.logs_dir / self._output_filename
        local_artifact_path.write_text(
            json.dumps(artifact, indent=2) + "\n"
        )
        sandbox_artifact_path = EnvironmentPaths.logs_dir / self._output_filename
        await environment.upload_file(
            local_artifact_path,
            sandbox_artifact_path.as_posix(),
        )
        artifact_written_at = datetime.now(timezone.utc).isoformat()

        metrics = (
            Metrics(
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                cached_tokens=usage.cache_tokens,
                cost_usd=usage.cost_usd,
                prompt_token_ids=response.prompt_token_ids,
                completion_token_ids=response.completion_token_ids,
                logprobs=response.logprobs,
            )
            if usage is not None
            else None
        )
        trajectory = Trajectory(
            session_id=self.session_id,
            agent=Agent(
                name=self.name(),
                version=self.version(),
                model_name=response.model_name or self.model_name,
            ),
            steps=[
                Step(
                    step_id=1,
                    timestamp=started_at,
                    source="user",
                    message=instruction,
                ),
                Step(
                    step_id=2,
                    timestamp=finished_at,
                    source="agent",
                    model_name=response.model_name or self.model_name,
                    message=response.content,
                    reasoning_content=response.reasoning_content,
                    metrics=metrics,
                    llm_call_count=1,
                ),
                Step(
                    step_id=3,
                    timestamp=artifact_written_at,
                    source="agent",
                    message=f"Wrote {self._output_filename}.",
                    tool_calls=[
                        ToolCall(
                            tool_call_id="write-result",
                            function_name="Write",
                            arguments={
                                "file_path": sandbox_artifact_path.as_posix(),
                                "content": "[structured LLM response]",
                            },
                        )
                    ],
                    observation=Observation(
                        results=[
                            ObservationResult(
                                source_call_id="write-result",
                                content="Artifact written.",
                            )
                        ]
                    ),
                    llm_call_count=0,
                ),
            ],
            final_metrics=FinalMetrics(
                total_prompt_tokens=usage.prompt_tokens if usage else None,
                total_completion_tokens=usage.completion_tokens if usage else None,
                total_cached_tokens=usage.cache_tokens if usage else None,
                total_cost_usd=usage.cost_usd if usage else None,
                total_steps=3,
            ),
        )
        (self.logs_dir / "trajectory.json").write_text(
            format_trajectory_json(trajectory.to_json_dict()) + "\n"
        )
