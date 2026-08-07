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
import logging
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, MutableMapping

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.prompt_seeds import seed_prompts
from oddish.core.prompts import resolve_prompt_core
from oddish.core.trial_io import (
    read_trial_instruction,
    read_trial_trajectory,
    read_trial_verifier_output,
)
from oddish.db.models import PromptKind, TrialModel

logger = logging.getLogger(__name__)

MAX_TEXT_CHARS = 2000
TRUNCATE_HEAD = 800
TRUNCATE_TAIL = 400
TRUNCATION_MARKER = "\n[...truncated {n} chars...]\n"
SCHEMA_VERSION = "5"
SUMMARY_PROMPT_KEY = PromptKind.TRAJECTORY_SUMMARY.value

# Output cap for the summary call. The Anthropic API requires max_tokens, so
# some value must be set; this one is a ceiling, not a target -- billing is on
# tokens actually generated. Was 2048 (inherited from the pre-migration cap),
# which truncated the model mid-JSON on long trajectories: a dump of 30 trials
# from experiment c02666c5 produced 13 parse failures whose raw output ended
# mid-token at ~5.3k chars, and those trials silently got no summary at all.
# Well under the model's own limit, so the binding constraint is the prompt's
# schema, not this number.
SUMMARY_MAX_TOKENS = 16384

# ``preprocess`` bounds each text field but nothing bounds the step *count*, so
# a long agent run still serializes past the model's input limit -- prod has
# seen 11.2M tokens against a 1M cap. Character count is not a usable preflight
# (a 588k-char prompt overflowed while a 2.17M-char one fit), so the API is the
# oracle: send it, and halve the step budget on each "prompt is too long" 400.
# A rejected request bills no tokens and returns in well under a second, so the
# ~96% of summaries that already fit pay nothing for this.
MAX_OVERFLOW_ATTEMPTS = 5
STEP_OMISSION_MARKER = "[{n} steps omitted to fit the context window]"
_CONTEXT_OVERFLOW_MARKERS = (
    "prompt is too long",
    "context length",
    "context_length_exceeded",
)


def _is_context_overflow(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _CONTEXT_OVERFLOW_MARKERS)


def clip_trajectory_steps(trajectory: dict, max_steps: int) -> dict:
    """Keep the first and last ``max_steps`` steps, dropping the middle.

    Head and tail carry the setup and the outcome -- the two things a summary
    has to get right. The dropped span is replaced by a single marker step with
    no ``step_id``, so it renders in the prompt but cannot be cited: the model
    can only reference steps that survive into ``_valid_step_ids``.

    The marker inherits the timestamp of the last dropped step. ``to_summary``
    derives each step's ``duration_ms`` from its predecessor in the list it is
    given, so without this the first retained tail step measures against a
    timestampless marker and contributes 0 to its component -- silently
    undercounting a duration the callers are told is safe to aggregate.
    """
    steps = trajectory.get("steps") or []
    if len(steps) <= max_steps:
        return trajectory
    head = max_steps // 2
    tail = max_steps - head
    omitted = len(steps) - max_steps
    last_dropped = steps[len(steps) - tail - 1]
    out = dict(trajectory)
    out["steps"] = [
        *steps[:head],
        {
            "step_id": None,
            "source": "system",
            "message": STEP_OMISSION_MARKER.format(n=omitted),
            "timestamp": (
                last_dropped.get("timestamp")
                if isinstance(last_dropped, dict)
                else None
            ),
        },
        *steps[len(steps) - tail :],
    ]
    return out


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
                k: _truncate(v) if isinstance(v, str) else v for k, v in args.items()
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

    return to_anthropic_api_model_id(settings.analysis_model) or settings.analysis_model


async def _load_summary_prompt(
    session: AsyncSession,
    *,
    org_id: str | None = None,
    user_id: str | None = None,
    experiment_id: str | None = None,
    task_id: str | None = None,
    trial_id: str | None = None,
) -> tuple[str, int, str]:
    """Load the latest trajectory-summary prompt, seeding built-ins on a miss.

    Resolves the narrowest scoped override for the given context, falling
    back to the global prompt when none exists. Returns ``(content, version,
    prompt_id)`` -- the id is required by callers so the block they build can
    stamp ``prompt_id`` and be attributed to the resolved row, not the global
    NULL-prompt_id fallback.
    """
    scope = dict(
        org_id=org_id,
        user_id=user_id,
        experiment_id=experiment_id,
        task_id=task_id,
        trial_id=trial_id,
    )
    try:
        prompt, version = await resolve_prompt_core(session, SUMMARY_PROMPT_KEY, **scope)
    except HTTPException:
        try:
            # Keep a lost concurrent-seed race inside a savepoint. Rolling back
            # the outer transaction would expire the caller's TrialModel.
            async with session.begin_nested():
                await seed_prompts(session)
            await session.commit()
            prompt, version = await resolve_prompt_core(session, SUMMARY_PROMPT_KEY, **scope)
        except Exception as exc:
            # An immediate unique constraint is raised inside the savepoint.
            # Its rollback leaves the outer transaction and loaded ORM state
            # intact, so the winner's row can be read without a full rollback.
            try:
                prompt, version = await resolve_prompt_core(
                    session, SUMMARY_PROMPT_KEY, **scope
                )
            except Exception:
                raise SummaryGenerationError(
                    f"prompt '{SUMMARY_PROMPT_KEY}' is not registered and "
                    f"seeding failed: {exc}"
                ) from exc
    return version.content, version.version, prompt.id


def build_summary_block(
    trajectory: dict,
    task_context: "TaskContext",
    *,
    analyzer_id: str | None,
    model: str,
    triggered_by_user_id: str | None = None,
    prompt_template: str,
    prompt_version: int,
    prompt_id: str,
):
    """Build the trajectory-summary ``AnalyzerBlock``.

    Single construction site shared by ``generate()`` (the production path)
    and the offline dump harness, so the two cannot drift in prompt, parser,
    or block metadata. The registry template and version are required so no
    production or offline caller can silently fall back to baked-in text.
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

    tb = TrajectoryBlock(
        TrajectoryInput(
            task_name=task_context.task_name,
            instruction=task_context.instruction,
            final_reward=task_context.final_reward,
            model_used=task_context.model_used,
            verifier_output=task_context.verifier_output,
            trajectory=trajectory,
        ),
        instructions_template=prompt_template,
    )
    return AnalyzerBlock(
        analyzer_type=AnalyzerType.TRAJECTORY_SUMMARY,
        llm_client_type=LLMClientType.API,
        input=AnalyzerInput(
            input={"trial_id": analyzer_id, "task_name": task_context.task_name}
        ),
        prompt=tb.build_prompt(),
        analyzer_id=analyzer_id,
        block_metadata={
            "schema_version": SCHEMA_VERSION,
            "model": model,
            "prompt_key": SUMMARY_PROMPT_KEY,
            "prompt_version": prompt_version,
            "prompt_id": prompt_id,
        },
        output_transform=lambda raw: tb.to_summary(raw, model=model),
        model=model,
        max_tokens=SUMMARY_MAX_TOKENS,
        triggered_by_user_id=triggered_by_user_id,
    )


async def generate(
    trajectory: dict,
    task_context: "TaskContext",
    *,
    analyzer_id: str | None = None,
    triggered_by_user_id: str | None = None,
    prompt_template: str,
    prompt_version: int,
    prompt_id: str,
) -> dict:
    """Run the trajectory summary as an ``AnalyzerBlock`` and return the dict.

    Builds the block via ``build_summary_block`` (shared with the offline dump
    harness), streams it -- the block self-persists to ``analyzer_blocks`` +
    S3 -- and returns the parsed ``schema_version=5`` summary. Raises
    ``SummaryGenerationError`` on any generation/parse failure.

    A trajectory that overflows the model's input limit is retried with half
    the steps, up to ``MAX_OVERFLOW_ATTEMPTS`` times. Every attempt persists its
    own ``analyzer_blocks`` row, so the shrink sequence stays auditable.
    """
    model = resolve_summary_model()
    total_steps = len(trajectory.get("steps") or [])
    budget: int | None = None

    for attempt in range(MAX_OVERFLOW_ATTEMPTS):
        payload = (
            trajectory if budget is None else clip_trajectory_steps(trajectory, budget)
        )
        block = build_summary_block(
            payload,
            task_context,
            analyzer_id=analyzer_id,
            model=model,
            triggered_by_user_id=triggered_by_user_id,
            prompt_template=prompt_template,
            prompt_version=prompt_version,
            prompt_id=prompt_id,
        )
        try:
            out = await block.run()
        except Exception as e:
            next_budget = max(1, (total_steps if budget is None else budget) // 2)
            # budget == 1 and still overflowing means the steps are not what is
            # oversized (a huge instruction or verifier log), so halving again
            # only burns attempts.
            exhausted = attempt == MAX_OVERFLOW_ATTEMPTS - 1 or budget == 1
            if not _is_context_overflow(e) or exhausted:
                raise SummaryGenerationError(f"summary block failed: {e}") from e
            logger.warning(
                "trajectory summary overflowed for analyzer_id=%s; retrying with "
                "%d of %d steps (attempt %d)",
                analyzer_id,
                next_budget,
                total_steps,
                attempt + 2,
            )
            budget = next_budget
            continue
        return out.output

    raise AssertionError("unreachable: the loop returns or raises")  # pragma: no cover


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
    session: AsyncSession, trial: TrialModel, triggered_by_user_id: str | None = None
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

    # Use the same "has a trajectory" notion as the trajectory endpoint (true
    # for finished Grok Build runs whose grok-build.json synthesizes to ATIF),
    # not just the raw has_trajectory column — otherwise those trials have a
    # fetchable trajectory but no summary.
    from oddish.core.helpers import _has_fetchable_trajectory

    if not _has_fetchable_trajectory(trial):
        return None

    async with _GEN_LOCKS[trial.id]:
        # Re-check inside the lock — another coroutine may have generated one.
        fresh = await _load_fresh_summary_block(session, trial.id)
        if fresh is not None:
            return fresh

        prompt_template, prompt_version, prompt_id = await _load_summary_prompt(
            session,
            org_id=trial.org_id,
            user_id=trial.billed_user_id,
            experiment_id=trial.experiment_id,
            task_id=trial.task_id,
            trial_id=trial.id,
        )

        trajectory, task_context = await asyncio.gather(
            read_trial_trajectory(trial),
            build_task_context(trial),
        )
        if trajectory is None:
            return None

        summary = await generate(
            trajectory,
            task_context,
            analyzer_id=trial.id,
            triggered_by_user_id=triggered_by_user_id,
            prompt_template=prompt_template,
            prompt_version=prompt_version,
            prompt_id=prompt_id,
        )

        # Mirror into the trials column for the graph builder + analyzer-input
        # bundles, which read it synchronously via getattr.
        await session.execute(
            update(TrialModel)
            .where(TrialModel.id == trial.id)
            .values(trajectory_summary=summary)
        )
        await session.commit()
        return summary
