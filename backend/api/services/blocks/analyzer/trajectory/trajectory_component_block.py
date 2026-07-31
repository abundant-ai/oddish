from __future__ import annotations

import enum
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, model_validator

from api.services.blocks.analyzer.trajectory import trajectory_prompts as tp
from oddish.blocks.block import Block

logger = logging.getLogger("oddish.trajectory_block")


class ExploreTrajectoryBlockTaxonomy(str, enum.Enum):
    READING_FILES = "reading_files"
    THINKING_RECALL = "thinking_recall"
    THINKING_UNDERSTAND = "thinking_understand"
    THINKING_HYPOTHESIZE = "thinking_hypothesize"
    THINKING_DIAGNOSE = "thinking_diagnose"
    THINKING_CORRECTION = "thinking_correction"


class ImplementTrajectoryBlockTaxonomy(str, enum.Enum):
    IMPLEMENTING = "implementing"
    IMPLEMENTING_CORRECTION = "implementing_correction"
    WRITING_TESTS = "writing_tests"
    TESTING_PUBLIC = "testing_public"
    TESTING_CUSTOM = "testing_custom"
    TESTING_CUSTOM_EDGE_CASES = "testing_custom_edge_cases"
    DEBUGGING = "debugging"


# One flat vocabulary built from the two sub-enums, so a component carries a
# single taxonomy value. Built from the members above to avoid value drift.
TrajectoryBlockTaxonomy = enum.Enum(
    "TrajectoryBlockTaxonomy",
    {
        m.name: m.value
        for m in (*ExploreTrajectoryBlockTaxonomy, *ImplementTrajectoryBlockTaxonomy)
    },
    type=str,
)


@dataclass
class TrajectoryComponent:
    step_ids: list[int]
    trajectory_component: TrajectoryBlockTaxonomy
    summary: Optional[str] = None
    tool_count: int = 0
    duration_ms: int = 0


class TrajectoryInput(BaseModel):
    task_name: str
    instruction: str | None = None
    final_reward: float | None = None
    model_used: str | None = None
    verifier_output: str | None = None
    trajectory: dict


class TrajectoryComponentModel(BaseModel):
    step_ids: list[int] = []
    trajectory_component: TrajectoryBlockTaxonomy
    summary: str | None = None


class TrajectoryOutput(BaseModel):
    # One bad element must not fail the whole parse: the pre-validator below drops
    # non-dict entries and coerces summary, so a single stray list item (LLMs
    # occasionally emit one) degrades gracefully. filter_output then does the
    # strict per-element validation + step-id filtering.
    summary: str = ""
    highlights: list[dict] = []
    components: list[dict] = []

    @model_validator(mode="before")
    @classmethod
    def _drop_bad_elements(cls, data):
        if not isinstance(data, dict):
            return data
        data = dict(data)
        data["summary"] = str(data.get("summary") or "")
        data["highlights"] = [
            h for h in (data.get("highlights") or []) if isinstance(h, dict)
        ]
        data["components"] = [
            c for c in (data.get("components") or []) if isinstance(c, dict)
        ]
        return data


_MAX_TEXT_CHARS = 2000
_TRUNCATE_HEAD = 800
_TRUNCATE_TAIL = 400


def _truncate(text: str) -> str:
    if len(text) <= _MAX_TEXT_CHARS:
        return text
    omitted = len(text) - _TRUNCATE_HEAD - _TRUNCATE_TAIL
    return (
        text[:_TRUNCATE_HEAD]
        + f"\n[...truncated {omitted} chars...]\n"
        + text[-_TRUNCATE_TAIL:]
    )


class _PreambleIn(BaseModel):
    pass


class _TaskIn(BaseModel):
    task_name: str
    instruction: str | None = None


class _OutcomeIn(BaseModel):
    final_reward: float | None = None
    model_used: str | None = None
    verifier_output: str | None = None


class _InstructionsIn(BaseModel):
    pass


class _TrajectoryIn(BaseModel):
    trajectory: dict


class TrajectoryBlock(Block):
    """Trajectory-specific block: build_prompt/parse are inherited from Block;
    this supplies the sections, output schema, and per-element cleanup."""

    output_schema = TrajectoryOutput

    def __init__(
        self, trajectory_input: TrajectoryInput, *, instructions_template: str
    ) -> None:
        self.trajectory_input = trajectory_input
        self._instructions_template = instructions_template

    # ---- prompt sections (build_prompt is inherited) ----
    def sections(self) -> list[dict]:
        ti = self.trajectory_input
        taxonomy_values = [m.value for m in TrajectoryBlockTaxonomy]
        return [
            {
                "name": "preamble",
                "raw_input": {},
                "schema": _PreambleIn,
                "formatter": lambda _d: tp.PREAMBLE,
            },
            {
                "name": "task",
                "raw_input": {"task_name": ti.task_name, "instruction": ti.instruction},
                "schema": _TaskIn,
                "formatter": self._fmt_task,
            },
            {
                "name": "outcome",
                "raw_input": {
                    "final_reward": ti.final_reward,
                    "model_used": ti.model_used,
                    "verifier_output": ti.verifier_output,
                },
                "schema": _OutcomeIn,
                "formatter": self._fmt_outcome,
            },
            {
                "name": "instructions",
                "raw_input": {},
                "schema": _InstructionsIn,
                "formatter": lambda _d: tp.instructions_section(
                    self._instructions_template, taxonomy_values
                ),
            },
            {
                "name": "trajectory",
                "raw_input": {"trajectory": ti.trajectory},
                "schema": _TrajectoryIn,
                "formatter": self._fmt_trajectory,
            },
        ]

    @staticmethod
    def _fmt_task(d: _TaskIn) -> str:
        instruction = (
            _truncate(d.instruction) if d.instruction is not None else "[unavailable]"
        )
        return tp.task_section(d.task_name, instruction)

    @staticmethod
    def _fmt_outcome(d: _OutcomeIn) -> str:
        reward = f"{d.final_reward}" if d.final_reward is not None else "[unavailable]"
        verifier = (
            _truncate(d.verifier_output)
            if d.verifier_output is not None
            else "[unavailable]"
        )
        model = d.model_used or "[unavailable]"
        return tp.outcome_section(reward, verifier, model)

    @staticmethod
    def _fmt_trajectory(d: _TrajectoryIn) -> str:
        from api.services.summarize_trajectory import preprocess

        return tp.trajectory_section(json.dumps(preprocess(d.trajectory)))

    # ---- parsing (parse is inherited; this filters elements) ----
    def _valid_step_ids(self) -> set[int]:
        return {
            s.get("step_id")
            for s in (self.trajectory_input.trajectory.get("steps") or [])
            if isinstance(s.get("step_id"), int)
        }

    def filter_output(self, parsed: TrajectoryOutput) -> TrajectoryOutput:
        valid = self._valid_step_ids()
        highlights = [
            {
                "step_id": h["step_id"],
                "title": str(h.get("title") or "").strip(),
                "why": str(h.get("why") or "").strip(),
            }
            for h in parsed.highlights
            if isinstance(h, dict)
            and isinstance(h.get("step_id"), int)
            and h["step_id"] in valid
        ]
        components: list[dict] = []
        for c in parsed.components:
            try:
                m = TrajectoryComponentModel(**c)
            except Exception:
                logger.exception("dropping malformed trajectory component %r", c)
                continue
            ids = [s for s in m.step_ids if s in valid]
            if not ids:
                continue
            components.append(
                {
                    "step_ids": ids,
                    "trajectory_component": m.trajectory_component.value,
                    "summary": m.summary,
                }
            )
        return TrajectoryOutput(
            summary=str(parsed.summary or "").strip(),
            highlights=highlights,
            components=components,
        )

    def to_summary(self, raw: str, *, model: str) -> dict:
        out = self.parse(raw)
        steps = self.trajectory_input.trajectory.get("steps") or []
        step_by_id = {
            step.get("step_id"): (index, step)
            for index, step in enumerate(steps)
            if isinstance(step, dict) and isinstance(step.get("step_id"), int)
        }

        def timestamp_ms(step: dict) -> float | None:
            value = step.get("timestamp")
            if not isinstance(value, str):
                return None
            try:
                return (
                    datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
                    * 1000
                )
            except ValueError:
                return None

        def duration_ms(index: int, step: dict) -> int:
            if index == 0:
                return 0
            current = timestamp_ms(step)
            previous = (
                timestamp_ms(steps[index - 1])
                if isinstance(steps[index - 1], dict)
                else None
            )
            if current is None or previous is None:
                return 0
            return max(0, round(current - previous))

        components = []
        for component in out.components:
            component_steps = [
                step_by_id[step_id]
                for step_id in component["step_ids"]
                if step_id in step_by_id
            ]
            components.append(
                {
                    **component,
                    # These fields are derived from the immutable trajectory rather
                    # than supplied by the LLM, so consumers can safely aggregate them.
                    "tool_count": sum(
                        len(step.get("tool_calls") or [])
                        for _, step in component_steps
                        if isinstance(step.get("tool_calls"), list)
                    ),
                    "duration_ms": sum(
                        duration_ms(index, step) for index, step in component_steps
                    ),
                }
            )
        return {
            "schema_version": "5",
            "model": model,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": out.summary,
            "highlights": out.highlights,
            "components": components,
        }
