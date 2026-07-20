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


_VERDICT_SECTION_NAME = "verdict"
_FALLBACK_SENTINEL = f"<{_VERDICT_SECTION_NAME}>[unavailable]</{_VERDICT_SECTION_NAME}>"


class VerdictBlock(Block):
    """Verdict-synthesis block: parse is inherited from Block; this supplies
    the single prompt section (the shared verdict prompt) and the output
    schema.

    Unlike trajectory/, where a degraded section is one of many, this
    section *is* the entire prompt. Block.render_section swallows any
    formatter exception into a `<verdict>[unavailable]</verdict>` fallback
    sentinel so one bad section can't sink an otherwise-fine multi-section
    prompt -- but here that fallback would silently become the whole judge
    prompt, and the judge would return a confident-looking verdict about
    nothing, persisted as SUCCESS. build_prompt() is overridden to detect
    that sentinel and raise instead, so a raising build_verdict_prompt fails
    this path exactly as loudly as it fails the legacy path (which calls it
    outside any try/except)."""

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

    # ---- prompt sections ----
    def sections(self) -> list[dict]:
        return [
            {
                "name": _VERDICT_SECTION_NAME,
                "raw_input": {},
                "schema": _EmptyInput,
                "formatter": lambda _d: vp.verdict_section(
                    self.classifications, self.baseline, self.quality_check_passed
                ),
            },
        ]

    def build_prompt(self) -> str:
        prompt = super().build_prompt()
        if prompt == _FALLBACK_SENTINEL:
            raise RuntimeError(
                "VerdictBlock's verdict section failed to render; refusing "
                "to send the placeholder fallback to the judge as if it "
                "were the real prompt"
            )
        return prompt

    # ---- parsing (parse is inherited) ----
    def to_verdict(self, raw: str) -> dict:
        return self.parse(raw).model_dump()
