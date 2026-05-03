"""LLM-backed trajectory summarization.

Two pure-ish responsibilities:
  - ``preprocess`` strips image content parts and truncates large text fields
    so the token cost of the summary call is bounded.
  - ``generate`` calls the Anthropic API with a preprocessed trajectory and
    returns a persistable summary dict.

This module deliberately mirrors the JSON-parsing style of
``oddish.worker.local_runner._run_probe_analyzer`` rather than using tool-use
so the test patterns and prompt-shape conventions match the rest of the repo.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, MutableMapping

from sqlalchemy import update
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
SCHEMA_VERSION = "2"
MODEL = "claude-sonnet-4-6"


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


def _strip_code_fences(text: str) -> str:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.lstrip().startswith("json"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        raw = raw.rsplit("```", 1)[0]
    return raw.strip()


def _render_prompt(trajectory: dict, task_context: "TaskContext") -> str:
    instruction = (
        _truncate(task_context.instruction)
        if task_context.instruction is not None
        else "[unavailable]"
    )
    verifier_output = (
        _truncate(task_context.verifier_output)
        if task_context.verifier_output is not None
        else "[unavailable]"
    )
    final_reward = (
        f"{task_context.final_reward}"
        if task_context.final_reward is not None
        else "[unavailable]"
    )
    model_used = task_context.model_used or "[unavailable]"

    return (
        "You are summarizing a recorded agent trajectory for a developer "
        "who wants a quick scan before diving into the per-step view.\n\n"
        f"<task>\n"
        f"Name: {task_context.task_name}\n"
        f"Instruction: {instruction}\n"
        f"</task>\n\n"
        f"<outcome>\n"
        f"Final reward: {final_reward}\n"
        f"Verifier output: {verifier_output}\n"
        f"Model: {model_used}\n"
        f"</outcome>\n\n"
        "Produce a 2-3 sentence summary covering what the agent set out "
        "to do, how it ended, and whether the verifier agreed. Then 3-6 "
        "pivotal 'key moments' with their step ids.\n\n"
        "Each highlight must reference a real `step_id` from the "
        "trajectory below. Pick steps where something genuinely shifted: "
        "a strategy was committed, a key tool call landed, an error "
        "redirected the work, or the final verdict was reached. Skip "
        "filler.\n\n"
        "Respond with ONLY a JSON object (no preamble, no code fences) "
        "matching this exact shape:\n"
        "{\n"
        '  "summary": "2-3 sentences",\n'
        '  "highlights": [\n'
        '    {"step_id": <int>, "title": "<short label>", '
        '"why": "<one sentence>"}\n'
        "  ]\n"
        "}\n"
        "Highlights must be ordered by step_id ascending.\n\n"
        f"<trajectory>\n{json.dumps(preprocess(trajectory))}\n</trajectory>"
    )


async def generate(trajectory: dict, task_context: "TaskContext") -> dict:
    """Call Claude to produce a persistable summary dict for ``trajectory``.

    Raises ``SummaryGenerationError`` if the model returns malformed JSON
    or cannot be parsed. Highlights referencing step_ids that are not in
    the source trajectory are dropped silently.
    """
    from anthropic import AsyncAnthropic

    valid_step_ids = {
        step.get("step_id")
        for step in (trajectory.get("steps") or [])
        if isinstance(step.get("step_id"), int)
    }

    prompt = _render_prompt(trajectory, task_context)

    try:
        client = AsyncAnthropic()
        msg = await client.messages.create(
            model=MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        raise SummaryGenerationError(f"Anthropic API call failed: {e}") from e

    raw_text = ""
    for block in msg.content:
        if hasattr(block, "text"):
            raw_text += block.text
    raw_text = _strip_code_fences(raw_text)
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise SummaryGenerationError(f"Model returned non-JSON: {e}") from e

    if not isinstance(parsed, dict):
        raise SummaryGenerationError(
            f"Model returned {type(parsed).__name__}, expected object"
        )

    summary = str(parsed.get("summary") or "").strip()
    raw_highlights = parsed.get("highlights") or []
    highlights: list[dict] = []
    if isinstance(raw_highlights, list):
        for entry in raw_highlights:
            if not isinstance(entry, dict):
                continue
            step_id = entry.get("step_id")
            if not isinstance(step_id, int) or step_id not in valid_step_ids:
                continue
            highlights.append(
                {
                    "step_id": step_id,
                    "title": str(entry.get("title") or "").strip(),
                    "why": str(entry.get("why") or "").strip(),
                }
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "model": MODEL,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "highlights": highlights,
    }


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
# results in at most a few duplicate Anthropic calls, and the second
# write into the JSONB column is idempotent.
_GEN_LOCKS: MutableMapping[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def _is_fresh(summary: dict | None) -> bool:
    return (
        isinstance(summary, dict)
        and summary.get("schema_version") == SCHEMA_VERSION
    )


async def get_or_generate_summary(
    session: AsyncSession, trial: TrialModel
) -> dict | None:
    """Return the persisted trajectory summary, generating on miss.

    Returns ``None`` when the trial has no trajectory to summarize.
    Raises ``SummaryGenerationError`` if the LLM call fails.
    """
    if _is_fresh(trial.trajectory_summary):
        return trial.trajectory_summary

    if not trial.has_trajectory:
        return None

    async with _GEN_LOCKS[trial.id]:
        # Re-check inside the lock — another coroutine may have populated.
        await session.refresh(trial, attribute_names=["trajectory_summary"])
        if _is_fresh(trial.trajectory_summary):
            return trial.trajectory_summary

        trajectory, task_context = await asyncio.gather(
            read_trial_trajectory(trial),
            build_task_context(trial),
        )
        if trajectory is None:
            return None

        summary = await generate(trajectory, task_context)

        await session.execute(
            update(TrialModel)
            .where(TrialModel.id == trial.id)
            .values(trajectory_summary=summary)
        )
        await session.commit()
        return summary
