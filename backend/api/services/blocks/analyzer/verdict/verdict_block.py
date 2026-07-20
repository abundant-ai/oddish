from __future__ import annotations

from pydantic import BaseModel

from oddish.analyze.models import (
    BaselineValidation,
    TaskVerdictModel,
    TrialClassification,
)

from api.services.blocks.analyzer.verdict import verdict_prompts as vp
from api.services.blocks.block import Block


class _EmptyInput(BaseModel):
    pass


class VerdictBlock(Block):
    """Verdict-synthesis block: build_prompt/parse are inherited from Block;
    this supplies the single prompt section (the shared verdict prompt) and
    the output schema."""

    output_schema = TaskVerdictModel

    def __init__(
        self,
        classifications: list[TrialClassification],
        *,
        baseline: BaselineValidation | None = None,
        quality_check_passed: bool = True,
    ) -> None:
        self.classifications = classifications
        self.baseline = baseline
        self.quality_check_passed = quality_check_passed

    # ---- prompt sections (build_prompt is inherited) ----
    def sections(self) -> list[dict]:
        return [
            {
                "name": "verdict",
                "raw_input": {},
                "schema": _EmptyInput,
                "formatter": lambda _d: vp.verdict_section(
                    self.classifications, self.baseline, self.quality_check_passed
                ),
            },
        ]

    # ---- parsing (parse is inherited) ----
    def to_verdict(self, raw: str) -> dict:
        return self.parse(raw).model_dump()
