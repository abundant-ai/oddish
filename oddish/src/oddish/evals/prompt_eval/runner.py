"""Run one prompt template over the golden corpus with each configured agent.

Each kind is executed through the same code path production uses, so an eval
result reflects what the worker would actually do with the edited prompt:

- ``QA_POST_TRIAL`` — ``TrialClassifier`` with a ``prompt_template`` override,
  reading the case's frozen ``task/`` + ``trial/`` directories.
- ``QA_PRE_TRIAL`` — ``PreTrialBlock`` rendering + the worker-local Claude CLI
  client over the case's ``task/`` directory.
- ``VERDICT`` — the packaged verdict template formatted with the case's
  inputs, constrained to ``TaskVerdictModel``.

All of these need the ``claude`` CLI on PATH and ``ANTHROPIC_API_KEY`` or
``CLAUDE_CODE_OAUTH_TOKEN`` in the environment.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass

from .goldens import AgentSpec, EvalConfig, GoldenCase
from .grading import grade

# The classifier reports its own infrastructure failures as HARNESS_ERROR with
# these subtypes; a golden expecting a genuine HARNESS_ERROR (e.g. a trial with
# no result.json) never sees them, so they safely mark runner errors.
_CLASSIFIER_ERROR_SUBTYPES = {"Classification Failed", "Timeout"}


@dataclass(frozen=True)
class RunRecord:
    """One graded run of one case with one agent."""

    case: str
    agent: str
    model: str
    trial: int
    passed: bool
    detail: str
    error: bool = False


async def _run_post_trial(
    case: GoldenCase, template: str, agent: AgentSpec, config: EvalConfig
) -> dict:
    from oddish.analyze.classifier import TrialClassifier

    classifier = TrialClassifier(
        model=agent.model,
        timeout=int(config.timeout_seconds),
        prompt_template=template,
    )
    result = await classifier.classify_trial(
        case.trial_dir, case.task_dir, trial_agent=case.trial_agent
    )
    if (
        result.classification.value == "HARNESS_ERROR"
        and result.subtype in _CLASSIFIER_ERROR_SUBTYPES
    ):
        raise RuntimeError(f"classifier error ({result.subtype}): {result.evidence}")
    return {"classification": result.classification.value, "subtype": result.subtype}


async def _run_claude_cli(
    prompt: str, *, cwd, schema: dict, agent: AgentSpec, config: EvalConfig
) -> str:
    from oddish.blocks.analyzer.claude_cli_client import ClaudeCliClient, CliConfig

    client = ClaudeCliClient(
        model=agent.model,
        config=CliConfig(
            cwd=cwd,
            json_schema=json.dumps(schema),
            timeout=config.timeout_seconds,
        ),
    )
    try:
        return "".join([chunk async for chunk in client.stream(prompt)])
    finally:
        await client.aclose()


async def _run_pre_trial(
    case: GoldenCase, template: str, agent: AgentSpec, config: EvalConfig
) -> dict:
    from oddish.analyze.models import PreTrialActionItems
    from oddish.blocks.analyzer.pre_trial.pre_trial_block import PreTrialBlock

    block = PreTrialBlock(task_id=case.name, trial_ids=[], prompt_template=template)
    raw = await _run_claude_cli(
        block.build_prompt(),
        cwd=case.task_dir,
        schema=PreTrialActionItems.model_json_schema(),
        agent=agent,
        config=config,
    )
    return block.to_action_items_from_cli(raw)


async def _run_verdict(
    case: GoldenCase, template: str, agent: AgentSpec, config: EvalConfig
) -> dict:
    from oddish.analyze.models import TaskVerdictModel
    from oddish.blocks.analyzer.claude_cli_client import parse_stream_json_result

    prompt = template.format(**case.inputs)
    raw = await _run_claude_cli(
        prompt,
        cwd=case.case_dir,
        schema=TaskVerdictModel.model_json_schema(),
        agent=agent,
        config=config,
    )
    return TaskVerdictModel.model_validate(parse_stream_json_result(raw)).model_dump()


_RUNNERS = {
    "QA_POST_TRIAL": _run_post_trial,
    "QA_PRE_TRIAL": _run_pre_trial,
    "VERDICT": _run_verdict,
}


async def run_template(
    kind: str,
    template: str,
    cases: list[GoldenCase],
    config: EvalConfig,
) -> list[dict]:
    """Every case x agent x trial for one template version. Returns JSON-ready
    ``RunRecord`` dicts; a run that errors is recorded (``error=True``,
    ``passed=False``) rather than sinking its siblings."""
    runner = _RUNNERS.get(kind)
    if runner is None:
        raise ValueError(f"no runner for prompt kind {kind!r}")

    semaphore = asyncio.Semaphore(max(1, config.concurrency))

    async def one(case: GoldenCase, agent: AgentSpec, trial: int) -> RunRecord:
        async with semaphore:
            try:
                output = await runner(case, template, agent, config)
                graded = grade(kind, case.expected, output)
                return RunRecord(
                    case=case.name,
                    agent=agent.name,
                    model=agent.model,
                    trial=trial,
                    passed=graded.passed,
                    detail=graded.detail,
                )
            except Exception as exc:  # noqa: BLE001 — recorded per-run
                return RunRecord(
                    case=case.name,
                    agent=agent.name,
                    model=agent.model,
                    trial=trial,
                    passed=False,
                    detail=f"run error: {exc}",
                    error=True,
                )

    records = await asyncio.gather(
        *(
            one(case, agent, trial)
            for case in cases
            for agent in config.agents
            for trial in range(1, config.n_trials + 1)
        )
    )
    return [asdict(record) for record in records]
