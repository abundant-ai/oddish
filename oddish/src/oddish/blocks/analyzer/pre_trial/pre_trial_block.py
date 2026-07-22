from __future__ import annotations

from pydantic import BaseModel

from oddish.blocks.block import Block
from oddish.analyze.models import PreTrialActionItems

from . import pre_trial_prompts as pp

_SECTION_NAME = "pre_trial"
_FALLBACK_SENTINEL = f"<{_SECTION_NAME}>[unavailable]</{_SECTION_NAME}>"


class _EmptyInput(BaseModel):
    pass


class PreTrialBlock(Block):
    """Audits task source for verifier/oracle/info-leakage defects and emits
    a list of ActionItems. The agent runs ``oddish pull`` in its sandbox."""

    output_schema = PreTrialActionItems

    def __init__(self, task_id: str, trial_ids: list[str], prompt_template: str) -> None:
        self.task_id = task_id
        self.trial_ids = trial_ids
        self.prompt_template = prompt_template

    def sections(self) -> list[dict]:
        return [
            {
                "name": _SECTION_NAME,
                "raw_input": {},
                "schema": _EmptyInput,
                "formatter": lambda _d: pp.pre_trial_section(
                    self.task_id, self.trial_ids, self.prompt_template
                ),
                "fallback": _FALLBACK_SENTINEL,
            }
        ]

    def build_prompt(self) -> str:
        prompt = super().build_prompt()
        if prompt == _FALLBACK_SENTINEL:
            raise RuntimeError("pre-trial prompt degraded to fallback sentinel")
        return prompt

    @classmethod
    def parse_json(cls, text: str):
        # The registry prompt asks for "the structured list of action items";
        # models sometimes return a bare JSON array instead of the
        # {"items": [...]} envelope Block.parse requires. Wrap it rather than
        # failing a valid audit on shape alone.
        data = super().parse_json(text)
        if isinstance(data, list):
            return {"items": data}
        return data

    def to_action_items(self, raw: str) -> dict:
        return self.parse(raw).model_dump()
