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
from pathlib import Path
from typing import Any, MutableMapping

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import oddish.analyze as _analyze
from oddish.core.trial_io import (
    read_trial_instruction,
    read_trial_trajectory,
    read_trial_verifier_output,
)
from oddish.db.models import (
    TrialModel,
    WorkerJobKind,
    WorkerJobModel,
)

logger = logging.getLogger(__name__)

MAX_TEXT_CHARS = 2000
TRUNCATE_HEAD = 800
TRUNCATE_TAIL = 400
TRUNCATION_MARKER = "\n[...truncated {n} chars...]\n"
SCHEMA_VERSION = "6"


def taxonomy_version() -> str:
    """Fingerprint of the label semantics a stored summary was produced under.

    Imported lazily-ish at call time so this module keeps its existing import
    shape; ``trajectory_prompts`` is pure text with no back-import.
    """
    from api.services.blocks.analyzer.trajectory.trajectory_prompts import (
        taxonomy_fingerprint,
    )

    return taxonomy_fingerprint()


def is_fresh_summary(summary: object) -> bool:
    """Whether a stored summary matches the schema AND the label vocabulary.

    ``schema_version`` alone was not enough. It gates the response *shape*, so
    retiring or redefining a label -- which changes what the numbers mean
    without changing their shape -- left every cached summary serving the old
    vocabulary indefinitely. Production summaries still carry
    `thinking_diagnose` and `testing_custom_edge_cases`, labels the enum no
    longer offers, and any comparison across time silently mixes them.
    """
    if not isinstance(summary, dict):
        return False
    if summary.get("schema_version") != SCHEMA_VERSION:
        return False
    return summary.get("taxonomy_version") == taxonomy_version()

# Must retain the ``{{taxonomy}}`` placeholder rendered by ``TrajectoryBlock``.
_SUMMARY_PROMPT_PATH = (
    Path(_analyze.__file__).resolve().parent / "prompts" / "trajectory_summary.txt"
)


def load_summary_prompt_template() -> str:
    """Read the packaged trajectory-summary prompt template."""
    return _SUMMARY_PROMPT_PATH.read_text()

# Output cap for the summary call. The Anthropic API requires max_tokens, so
# some value must be set; this one is a ceiling, not a target -- billing is on
# tokens actually generated. Was 2048 (inherited from the pre-migration cap),
# which truncated the model mid-JSON on long trajectories: a dump of 30 trials
# from experiment c02666c5 produced 13 parse failures whose raw output ended
# mid-token at ~5.3k chars, and those trials silently got no summary at all.
# Well under the model's own limit, so the binding constraint is the prompt's
# schema, not this number.
SUMMARY_MAX_TOKENS = 16384

# ``preprocess`` bounds each text field but nothing bounds the step *count* or
# a step's total text, so a long agent run still serializes past the model's
# input limit -- prod has seen 11.2M tokens against a 1M cap. Character count is
# not a usable preflight (a 588k-char prompt overflowed while a 2.17M-char one
# fit), so the API is the oracle: send it, and shrink on each "prompt is too
# long" 400. A rejected request bills no tokens and returns in well under a
# second, so the ~96% of summaries that already fit pay nothing for this.
#
# Text shrinks before steps drop. Dropping steps is not free the way truncating
# text is: ``clip_trajectory_steps`` replaces the middle with a marker that has
# no ``step_id``, and the model can only cite steps that survive into
# ``_valid_step_ids`` -- so every dropped step lands in no component and the UI
# files it under "Other". Trial kubernetes-rust-rewrite-bae0f616-417 measured
# the cost: 2,670,311 tokens over 1377 steps (~1.9k tokens/step) halved twice to
# 344 steps, of which the model labelled 343. Coverage was 24.9% not because the
# model gave up but because it was shown a quarter of the run.
#
# At ~1.9k tokens/step the text is the whole weight, so shrinking it buys back
# steps directly. Measured on that step shape, the ceiling under a 1M cap goes
# 495 steps unshrunk -> 1,785 / 3,847 / 5,884 across the three rungs. The 1377
# steps fit whole on the first rung at 766k tokens. Prod's largest recorded
# overflow, 11.2M tokens, is ~5.5k steps at that density and lands inside the
# last rung -- narrowly, so clipping stays wired up behind it.
TEXT_SHRINK_RUNGS: tuple[tuple[int, int], ...] = ((600, 2400), (200, 800), (80, 320))
MAX_OVERFLOW_ATTEMPTS = 8
STEP_OMISSION_MARKER = "[{n} steps omitted to fit the context window]"
_CONTEXT_OVERFLOW_MARKERS = (
    "prompt is too long",
    "context length",
    "context_length_exceeded",
)


def _is_context_overflow(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _CONTEXT_OVERFLOW_MARKERS)


def _shrink_ladder(
    total_steps: int,
) -> list[tuple[tuple[int, int] | None, int | None]]:
    """``(text_rung, step_budget)`` per overflow attempt, widest first.

    The step rungs carry the tightest text rung with them: once steps are being
    dropped, every character saved buys back another step the model can cite.
    Halving stops at 1 -- still overflowing there means the steps are not what
    is oversized (a huge instruction or verifier log), so further rungs would
    only burn attempts.
    """
    ladder: list[tuple[tuple[int, int] | None, int | None]] = [(None, None)]
    ladder += [(rung, None) for rung in TEXT_SHRINK_RUNGS]
    budget = total_steps
    while len(ladder) < MAX_OVERFLOW_ATTEMPTS and budget > 1:
        budget = max(1, budget // 2)
        ladder.append((TEXT_SHRINK_RUNGS[-1], budget))
    return ladder


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


def _truncate(text: str, budget: int = MAX_TEXT_CHARS) -> str:
    if len(text) <= budget:
        return text
    # Head/tail scale with the budget so a tighter rung keeps the same 2:1
    # opening-to-ending shape the default 800/400 split has.
    head = max(1, budget * TRUNCATE_HEAD // MAX_TEXT_CHARS)
    tail = max(1, budget * TRUNCATE_TAIL // MAX_TEXT_CHARS)
    omitted = len(text) - head - tail
    return text[:head] + TRUNCATION_MARKER.format(n=omitted) + text[-tail:]


def _strip_images(parts: list[dict], budget: int) -> list[dict]:
    """Replace image parts with a single placeholder text part."""
    out: list[dict] = []
    skipped = 0
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "image":
            skipped += 1
            continue
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text") or ""
            out.append({"type": "text", "text": _truncate(text, budget)})
        else:
            out.append(part)
    if skipped:
        out.append({"type": "text", "text": f"[image omitted] (x{skipped})"})
    return out


def _process_content(value: Any, budget: int = MAX_TEXT_CHARS) -> Any:
    """Process MessageContent / ObservationContent (string | list[ContentPart] | None)."""
    if value is None:
        return None
    if isinstance(value, str):
        return _truncate(value, budget)
    if isinstance(value, list):
        return _strip_images(value, budget)
    return value


def _process_tool_calls(
    tool_calls: list[dict] | None, budget: int = MAX_TEXT_CHARS
) -> list[dict] | None:
    if not tool_calls:
        return tool_calls
    out = []
    for call in tool_calls:
        new_call = dict(call)
        args = new_call.get("arguments")
        if isinstance(args, dict):
            new_call["arguments"] = {
                k: _truncate(v, budget) if isinstance(v, str) else v
                for k, v in args.items()
            }
        out.append(new_call)
    return out


def _process_observation(
    obs: dict | None, budget: int = MAX_TEXT_CHARS
) -> dict | None:
    if obs is None:
        return None
    new_obs = dict(obs)
    new_results = []
    for result in obs.get("results") or []:
        new_result = dict(result)
        new_result["content"] = _process_content(result.get("content"), budget)
        new_results.append(new_result)
    new_obs["results"] = new_results
    return new_obs


def _has_content(value: object) -> bool:
    """True when a MessageContent / ObservationContent carries substance.

    Both are ``str | list[ContentPart] | None``, so the list form has to be
    walked -- a step whose only substance is a content-part list is real. Mirror
    of the frontend's ``hasContent``: a text part counts when non-blank, any
    other part (an image) counts on its own.
    """
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(
            _has_content(part.get("text"))
            if isinstance(part, dict) and part.get("type") == "text"
            else bool(part)
            for part in value
        )
    return bool(value)


def _step_is_inert(step: dict) -> bool:
    """True when a step carries no content of any kind.

    Agent protocols emit empty turns between real ones -- ``{"step_id": 3,
    "source": "user", "message": ""}`` and the like. They are 71-88% of the
    steps in every trajectory measured, in runs as long as 366 consecutive
    steps, and they say nothing about what the agent did.

    Deliberately conservative: any tool call, any non-blank message,
    reasoning, or observation content keeps the step. The step-omission marker
    from ``clip_trajectory_steps`` carries a message, so it survives too.
    """
    if step.get("tool_calls"):
        return False
    if _has_content(step.get("message")):
        return False
    if _has_content(step.get("reasoning_content")):
        return False

    observation = step.get("observation")
    if isinstance(observation, dict):
        for result in observation.get("results") or []:
            if isinstance(result, dict):
                if _has_content(result.get("content")):
                    return False
            elif result:
                return False
    elif observation:
        return False
    return True


def drop_inert_steps(trajectory: dict) -> dict:
    """Return a copy of ``trajectory`` without its contentless steps.

    Applied only where the prompt is built, so it changes what the model reads
    and nothing else: ``to_summary`` and ``_valid_step_ids`` both key off the
    unfiltered ``TrajectoryInput.trajectory``, so durations, step indices, and
    citation validation are unaffected. Surviving steps keep their original
    ``step_id`` -- nothing is renumbered -- so a cited id still resolves.

    Steps the model never sees go unclaimed by any component, which the
    frontend already renders through its synthetic "unattributed" bucket.
    """
    steps = trajectory.get("steps") or []
    kept = [s for s in steps if not (isinstance(s, dict) and _step_is_inert(s))]
    if len(kept) == len(steps):
        return trajectory
    logger.info(
        "trajectory summary: dropped %d/%d contentless steps",
        len(steps) - len(kept),
        len(steps),
    )
    out = dict(trajectory)
    out["steps"] = kept
    return out


# A per-field share below this stops being a summary and starts being noise,
# so a step with very many fields may exceed ``max_step_chars`` rather than
# shred every one of them.
MIN_FIELD_CHARS = 80


def _process_step(step: dict, budget: int) -> dict:
    new_step = dict(step)
    new_step["message"] = _process_content(step.get("message"), budget)
    rc = step.get("reasoning_content")
    if isinstance(rc, str):
        new_step["reasoning_content"] = _truncate(rc, budget)
    new_step["tool_calls"] = _process_tool_calls(step.get("tool_calls"), budget)
    new_step["observation"] = _process_observation(step.get("observation"), budget)
    return new_step


def _text_field_lengths(step: dict) -> list[int]:
    """Lengths of every independently-truncatable text field on a step."""
    lengths: list[int] = []

    def add(value: Any) -> None:
        if isinstance(value, str):
            lengths.append(len(value))
        elif isinstance(value, list):
            lengths.extend(
                len(part.get("text") or "")
                for part in value
                if isinstance(part, dict) and part.get("type") == "text"
            )

    add(step.get("message"))
    add(step.get("reasoning_content"))
    for call in step.get("tool_calls") or []:
        args = call.get("arguments") if isinstance(call, dict) else None
        if isinstance(args, dict):
            lengths.extend(len(v) for v in args.values() if isinstance(v, str))
    obs = step.get("observation")
    if isinstance(obs, dict):
        for result in obs.get("results") or []:
            if isinstance(result, dict):
                add(result.get("content"))
    return lengths


def preprocess(
    trajectory: dict,
    *,
    max_text_chars: int = MAX_TEXT_CHARS,
    max_step_chars: int | None = None,
) -> dict:
    """Return a copy of ``trajectory`` with images stripped and long text truncated.

    ``max_step_chars`` bounds what one *step* carries. A per-field budget alone
    does not: a step with a dozen tool-call arguments and a dozen observation
    results pays the field budget a dozen times over, and that skew is what puts
    a long run at ~1.9k tokens/step. Fields over quota are re-truncated to an
    even share of the step budget rather than proportionally, so one runaway
    field cannot crowd out the rest of the step.
    """
    out = deepcopy(trajectory)
    new_steps = []
    for step in out.get("steps") or []:
        new_step = _process_step(step, max_text_chars)
        if max_step_chars is not None:
            lengths = _text_field_lengths(new_step)
            if sum(lengths) > max_step_chars:
                share = max(MIN_FIELD_CHARS, max_step_chars // max(len(lengths), 1))
                new_step = _process_step(step, share)
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


def build_summary_block(
    trajectory: dict,
    task_context: "TaskContext",
    *,
    analyzer_id: str | None,
    model: str,
    triggered_by_user_id: str | None = None,
    prompt_template: str | None = None,
):
    """Build the trajectory-summary ``AnalyzerBlock``.

    Single construction site shared by ``generate()`` (the production path)
    and the offline dump harness, so the two cannot drift in prompt, parser,
    or block metadata. ``prompt_template`` defaults to the packaged template;
    the dump harness may pass an experimental one.
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
        instructions_template=prompt_template or load_summary_prompt_template(),
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
            "taxonomy_version": taxonomy_version(),
            "model": model,
        },
        output_transform=lambda raw: tb.to_summary(raw, model=model),
        model=model,
        max_tokens=SUMMARY_MAX_TOKENS,
        response_format=tb.output_schema,
        output_schema=tb.output_schema.model_json_schema(),
        triggered_by_user_id=triggered_by_user_id,
    )


async def generate(
    trajectory: dict,
    task_context: "TaskContext",
    *,
    analyzer_id: str | None = None,
    triggered_by_user_id: str | None = None,
    prompt_template: str | None = None,
) -> dict:
    """Run the trajectory summary as an ``AnalyzerBlock`` and return the dict.

    Builds the block via ``build_summary_block`` (shared with the offline dump
    harness), streams it -- the block self-persists to ``analyzer_blocks`` +
    S3 -- and returns the parsed ``schema_version=5`` summary. Raises
    ``SummaryGenerationError`` on any generation/parse failure.

    A trajectory that overflows the model's input limit walks the shrink ladder
    from ``_shrink_ladder``: text budgets first, then step clipping, capped at
    ``MAX_OVERFLOW_ATTEMPTS`` rungs. Every attempt persists its own
    ``analyzer_blocks`` row, so the shrink sequence stays auditable.
    """
    model = resolve_summary_model()
    if prompt_template is None:
        # Read here rather than inside build_summary_block: callers of
        # generate() only handle SummaryGenerationError, so a missing or
        # unreadable packaged file must not escape as a raw OSError.
        try:
            prompt_template = load_summary_prompt_template()
        except OSError as e:
            raise SummaryGenerationError(f"summary template unavailable: {e}") from e
    ladder = _shrink_ladder(len(trajectory.get("steps") or []))
    last_prompt: str | None = None
    last_error: Exception | None = None

    for index, (text_rung, step_budget) in enumerate(ladder):
        payload = trajectory
        if text_rung is not None:
            payload = preprocess(
                payload, max_text_chars=text_rung[0], max_step_chars=text_rung[1]
            )
        if step_budget is not None:
            payload = clip_trajectory_steps(payload, step_budget)
        block = build_summary_block(
            payload,
            task_context,
            analyzer_id=analyzer_id,
            model=model,
            triggered_by_user_id=triggered_by_user_id,
            prompt_template=prompt_template,
        )
        # A text rung that shrinks nothing -- short steps, where the step
        # *count* is what overflows -- builds a byte-identical prompt. Skipping
        # it spends no round trip and leaves the step rungs their full depth.
        if block.prompt == last_prompt:
            continue
        last_prompt = block.prompt
        try:
            out = await block.run()
        except Exception as e:
            last_error = e
            if not _is_context_overflow(e) or index == len(ladder) - 1:
                raise SummaryGenerationError(f"summary block failed: {e}") from e
            logger.warning(
                "trajectory summary overflowed for analyzer_id=%s; retrying at "
                "rung %d of %d (text budget %s, %s steps)",
                analyzer_id,
                index + 2,
                len(ladder),
                ladder[index + 1][0],
                ladder[index + 1][1] or "all",
            )
            continue
        return out.output

    # Reachable only if every remaining rung built a prompt already sent.
    raise SummaryGenerationError(f"summary block failed: {last_error}")


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

    # ``awaitable_attrs``: callers reach here with trials loaded via bare
    # ``session.get`` (trajectory-summary endpoint, post-trial QA worker
    # hook), so the task relationship may not be eagerly loaded.
    task = await trial.awaitable_attrs.task
    task_name = task.name if task is not None else ""

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
                # Same bar as ``is_fresh_summary``: a block written under a
                # different label vocabulary is stale even at the right schema.
                AnalyzerBlockModel.output["taxonomy_version"].astext
                == taxonomy_version(),
            )
            .order_by(AnalyzerBlockModel.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def load_stored_summary(session: AsyncSession, trial) -> dict | None:
    """The stored trajectory summary for a trial, or None. Never generates.

    The read half of ``get_or_generate_summary``. Public reads use this before
    enqueueing paid work so a warm summary remains a cheap cache hit.

    Falls back to the ``trials.trajectory_summary`` mirror, for the same reason
    ``resolve_cohorts`` prefers it: ``preview_seed`` copies trials but not
    ``analyzer_blocks``, so a block-only read is empty on every preview deploy
    even though the summary is sitting on the trial row. The authenticated
    route papers over that by generating; this one cannot, so a share page
    would simply show no summary.

    The mirror is held to the same freshness bar as the block -- it is written
    from the same output, so it carries the same ``schema_version``.
    """
    block = await _load_fresh_summary_block(session, trial.id)
    if block is not None:
        return block
    mirror = getattr(trial, "trajectory_summary", None)
    if is_fresh_summary(mirror):
        return mirror
    return None


async def get_or_enqueue_summary_job(
    session: AsyncSession,
    trial: TrialModel,
    *,
    triggered_by_user_id: str | None = None,
) -> WorkerJobModel:
    """Return the one durable generation job for this trial and schema.

    Locking the trial makes the read-then-insert atomic across API processes.
    A terminal failure is deliberately returned instead of silently enqueueing
    another paid LLM call on every anonymous page refresh. A schema bump forms
    a new idempotency key and is allowed to enqueue fresh work.
    """
    await session.execute(
        select(TrialModel.id).where(TrialModel.id == trial.id).with_for_update()
    )
    existing = (
        await session.execute(
            select(WorkerJobModel)
            .where(
                WorkerJobModel.kind == WorkerJobKind.ANALYZER,
                WorkerJobModel.subject_table == "trials",
                WorkerJobModel.subject_id == trial.id,
                WorkerJobModel.payload["mode"].astext == "trajectory_summary",
                WorkerJobModel.payload["schema_version"].astext == SCHEMA_VERSION,
            )
            .order_by(WorkerJobModel.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    from oddish.config import settings
    from oddish.workers.jobs import EnqueueRequest, enqueue_worker_job

    return await enqueue_worker_job(
        session,
        EnqueueRequest(
            kind=WorkerJobKind.ANALYZER,
            queue_key=settings.get_qa_queue_key(),
            priority=1,
            payload={
                "mode": "trajectory_summary",
                "trial_id": trial.id,
                "schema_version": SCHEMA_VERSION,
                "triggered_by_user_id": triggered_by_user_id,
            },
            subject_table="trials",
            subject_id=trial.id,
            org_id=trial.org_id,
        ),
    )


async def get_or_generate_summary(
    session: AsyncSession,
    trial: TrialModel,
    triggered_by_user_id: str | None = None,
    *,
    refresh: bool = False,
) -> dict | None:
    """Return the trajectory summary, generating on miss.

    Source of truth is the latest fresh SUCCESS trajectory_summary
    ``AnalyzerBlock`` for the trial; the result is mirrored into
    ``trials.trajectory_summary`` for the graph builder + analyzer-input readers.
    Returns ``None`` when the trial has no trajectory; raises
    ``SummaryGenerationError`` if generation fails.

    ``refresh`` skips the cache and always generates. The new block is written
    alongside the old one and wins on ``created_at``, so nothing is deleted and
    a failed regeneration leaves the previous summary serving.
    """
    fresh = None if refresh else await _load_fresh_summary_block(session, trial.id)
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
        # A refresh deliberately ignores that: it was asked for a new summary,
        # and the one waiting in front of it may be the stale block it wants
        # replaced. Two concurrent refreshes therefore generate twice; that is
        # an explicit, scoped operation, not something a page view can trigger.
        fresh = None if refresh else await _load_fresh_summary_block(session, trial.id)
        if fresh is not None:
            return fresh

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
