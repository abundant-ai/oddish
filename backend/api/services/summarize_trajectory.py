"""LLM-backed trajectory summarization.

Responsibilities:
  - ``preprocess`` strips image content parts and truncates large text fields
    so the token cost of the summary call is bounded.
  - ``generate`` runs the summary as an ``AnalyzerBlock`` over a
    ``TrajectoryBlock`` (prompt + parse) and returns a persistable summary dict.
  - ``get_or_generate_summary`` reads the latest fresh summary block for a trial
    (source of truth), generating + mirroring into ``trials.trajectory_summary``
    on a miss.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, MutableMapping

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.trial_io import (
    read_trial_instruction,
    read_trial_trajectory,
    read_trial_verifier_output,
)
from oddish.db.models import TrialModel

MAX_TEXT_CHARS = 2000
TRUNCATE_HEAD = 800
TRUNCATE_TAIL = 400
TRUNCATION_MARKER = "\n[...truncated {n} chars...]\n"
SCHEMA_VERSION = "4"


def _truncate(text: str) -> str:
    if len(text) <= MAX_TEXT_CHARS:
        return text
    head = text[:TRUNCATE_HEAD]
    tail = text[-TRUNCATE_TAIL:]
    omitted = len(text) - TRUNCATE_HEAD - TRUNCATE_TAIL
    return head + TRUNCATION_MARKER.format(n=omitted) + tail


def _strip_images(parts: list[dict]) -> list[dict]:
    """Replace image parts with a single placeholder text part."""
    out: list[dict] = []
    skipped = 0
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "image":
            skipped += 1
            continue
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text") or ""
            out.append({"type": "text", "text": _truncate(text)})
        else:
            out.append(part)
    if skipped:
        out.append({"type": "text", "text": f"[image omitted] (x{skipped})"})
    return out


def _process_content(value: Any) -> Any:
    """Process MessageContent / ObservationContent (string | list[ContentPart] | None)."""
    if value is None:
        return None
    if isinstance(value, str):
        return _truncate(value)
    if isinstance(value, list):
        return _strip_images(value)
    return value


def _process_tool_calls(tool_calls: list[dict] | None) -> list[dict] | None:
    if not tool_calls:
        return tool_calls
    out = []
    for call in tool_calls:
        new_call = dict(call)
        args = new_call.get("arguments")
        if isinstance(args, dict):
            new_call["arguments"] = {
                k: _truncate(v) if isinstance(v, str) else v
                for k, v in args.items()
            }
        out.append(new_call)
    return out


def _process_observation(obs: dict | None) -> dict | None:
    if obs is None:
        return None
    new_obs = dict(obs)
    new_results = []
    for result in obs.get("results") or []:
        new_result = dict(result)
        new_result["content"] = _process_content(result.get("content"))
        new_results.append(new_result)
    new_obs["results"] = new_results
    return new_obs


def preprocess(trajectory: dict) -> dict:
    """Return a copy of ``trajectory`` with images stripped and long text truncated."""
    out = deepcopy(trajectory)
    new_steps = []
    for step in out.get("steps") or []:
        new_step = dict(step)
        new_step["message"] = _process_content(step.get("message"))
        rc = step.get("reasoning_content")
        if isinstance(rc, str):
            new_step["reasoning_content"] = _truncate(rc)
        new_step["tool_calls"] = _process_tool_calls(step.get("tool_calls"))
        new_step["observation"] = _process_observation(step.get("observation"))
        new_steps.append(new_step)
    out["steps"] = new_steps
    return out


class SummaryGenerationError(RuntimeError):
    """Raised when the LLM returned content we could not turn into a summary."""


def resolve_summary_model() -> str:
    """The shared analysis model for trajectory summaries.

    Bedrock inference-profile ids are normalized back to the plain API id
    because the summary runs on the direct Anthropic API; plain ids pass
    through unchanged.
    """
    from oddish.config import settings, to_anthropic_api_model_id

    return (
        to_anthropic_api_model_id(settings.analysis_model)
        or settings.analysis_model
    )


def build_summary_block(
    trajectory: dict,
    task_context: "TaskContext",
    *,
    analyzer_id: str | None,
    model: str,
    client,
):
    """Build the trajectory-summary ``AnalyzerBlock``.

    Single construction site shared by ``generate()`` (the production path)
    and the offline dump harness, so the two cannot drift in prompt, parser,
    or block metadata.
    """
    from oddish.blocks.analyzer.analyzer_block import (
        AnalyzerBlock,
        AnalyzerInput,
        AnalyzerType,
    )
    from oddish.blocks.analyzer.analyzer_llm_client import LLMClientType
    from api.services.blocks.analyzer.trajectory.trajectory_component_block import (
        TrajectoryBlock,
        TrajectoryInput,
    )

    tb = TrajectoryBlock(TrajectoryInput(
        task_name=task_context.task_name,
        instruction=task_context.instruction,
        final_reward=task_context.final_reward,
        model_used=task_context.model_used,
        verifier_output=task_context.verifier_output,
        trajectory=trajectory,
    ))
    return AnalyzerBlock(
        analyzer_type=AnalyzerType.TRAJECTORY_SUMMARY,
        llm_client_type=LLMClientType.API,
        input=AnalyzerInput(
            input={"trial_id": analyzer_id, "task_name": task_context.task_name}
        ),
        prompt=tb.build_prompt(),
        analyzer_id=analyzer_id,
        block_metadata={"schema_version": SCHEMA_VERSION, "model": model},
        output_transform=lambda raw: tb.to_summary(raw, model=model),
        client=client,
    )


async def generate(
    trajectory: dict,
    task_context: "TaskContext",
    *,
    analyzer_id: str | None = None,
    client=None,
) -> dict:
    """Run the trajectory summary as an ``AnalyzerBlock`` and return the dict.

    Builds the block via ``build_summary_block`` (shared with the offline dump
    harness), streams it -- the block self-persists to ``analyzer_blocks`` +
    S3 -- and returns the parsed ``schema_version=4`` summary. Raises
    ``SummaryGenerationError`` on any generation/parse failure. ``client`` is
    injected in tests; otherwise a model-scoped ``ApiAnalyzerLLMClient`` is used.
    """
    from oddish.blocks.analyzer.analyzer_llm_client import ApiAnalyzerLLMClient

    model = resolve_summary_model()
    owned = client is None
    # 2048 is the pre-migration cap, and it only holds because the client pins
    # thinking off -- thinking shares this ceiling with the JSON body.
    llm = client or ApiAnalyzerLLMClient(model=model, max_tokens=2048)
    block = build_summary_block(
        trajectory, task_context, analyzer_id=analyzer_id, model=model, client=llm,
    )
    try:
        out = await block.run()
    except Exception as e:
        raise SummaryGenerationError(f"summary block failed: {e}") from e
    finally:
        if owned:
            await llm.aclose()
    return out.output


# ---------------------------------------------------------------------------
# Task context bundle (fed into the summary prompt)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskContext:
    """Bundle of task and outcome data fed into the summary prompt.

    Each field may be None — the prompt renders missing values as
    ``[unavailable]`` rather than failing generation.
    """

    task_name: str
    instruction: str | None
    final_reward: float | None
    model_used: str | None
    verifier_output: str | None


async def build_task_context(trial) -> TaskContext:
    """Assemble TaskContext from DB fields + parallel S3 reads.

    The two S3 reads (instruction.md, verifier/test-stdout.txt) run
    concurrently with each other.
    """
    instruction, verifier_output = await asyncio.gather(
        read_trial_instruction(trial),
        read_trial_verifier_output(trial),
    )

    model_used = trial.model
    if model_used is None and isinstance(trial.harbor_config, dict):
        agent_cfg = trial.harbor_config.get("agent")
        if isinstance(agent_cfg, dict):
            model_used = agent_cfg.get("model")

    task_name = trial.task.name if trial.task is not None else ""

    return TaskContext(
        task_name=task_name,
        instruction=instruction,
        final_reward=trial.reward,
        model_used=model_used,
        verifier_output=verifier_output,
    )


# ---------------------------------------------------------------------------
# DB-backed orchestrator
# ---------------------------------------------------------------------------

# Per-trial-id locks so two concurrent requests don't both kick off
# generation for the same trial. Process-local (Modal containers each
# get their own dict) — that's acceptable: cross-container racing
# results in at most a few duplicate generations, and the writes are idempotent.
_GEN_LOCKS: MutableMapping[str, asyncio.Lock] = defaultdict(asyncio.Lock)


async def _load_fresh_summary_block(
    session: AsyncSession, trial_id: str
) -> dict | None:
    """The latest fresh SUCCESS trajectory_summary block for a trial, or None.

    Source of truth for the summary: an ``analyzer_blocks`` row of type
    ``trajectory_summary`` whose output carries the current ``schema_version``.
    """
    from oddish.blocks.analyzer.analyzer_block import AnalyzerType
    from oddish.db.models import AnalyzerBlockModel, JobStatus

    return (
        await session.execute(
            select(AnalyzerBlockModel.output)
            .where(
                AnalyzerBlockModel.analyzer_id == trial_id,
                AnalyzerBlockModel.type == AnalyzerType.TRAJECTORY_SUMMARY.value,
                AnalyzerBlockModel.status == JobStatus.SUCCESS,
                AnalyzerBlockModel.output["schema_version"].astext == SCHEMA_VERSION,
            )
            .order_by(AnalyzerBlockModel.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def get_or_generate_summary(
    session: AsyncSession, trial: TrialModel
) -> dict | None:
    """Return the trajectory summary, generating on miss.

    Source of truth is the latest fresh SUCCESS trajectory_summary
    ``AnalyzerBlock`` for the trial; the result is mirrored into
    ``trials.trajectory_summary`` for the graph builder + analyzer-input readers.
    Returns ``None`` when the trial has no trajectory; raises
    ``SummaryGenerationError`` if generation fails.
    """
    fresh = await _load_fresh_summary_block(session, trial.id)
    if fresh is not None:
        return fresh

    # Use the same "has a trajectory" notion as the trajectory endpoint /
    # trajectory-graph gate (true for finished Grok Build runs whose
    # grok-build.json synthesizes to ATIF), not just the raw has_trajectory
    # column — otherwise those trials get an Agent Graph but no summary.
    from oddish.core.helpers import _has_fetchable_trajectory

    if not _has_fetchable_trajectory(trial):
        return None

    async with _GEN_LOCKS[trial.id]:
        # Re-check inside the lock — another coroutine may have generated one.
        fresh = await _load_fresh_summary_block(session, trial.id)
        if fresh is not None:
            return fresh

        trajectory, task_context = await asyncio.gather(
            read_trial_trajectory(trial),
            build_task_context(trial),
        )
        if trajectory is None:
            return None

        summary = await generate(trajectory, task_context, analyzer_id=trial.id)

        # Mirror into the trials column for the graph builder + analyzer-input
        # bundles, which read it synchronously via getattr.
        await session.execute(
            update(TrialModel)
            .where(TrialModel.id == trial.id)
            .values(trajectory_summary=summary)
        )
        await session.commit()
        return summary
