from __future__ import annotations

import logging

from pydantic import BaseModel, ValidationError

from oddish.blocks.analyzer.claude_cli_client import parse_stream_json_result
from oddish.blocks.block import Block, BlockParseError
from oddish.analyze.models import ActionItem, PreTrialActionItems

from . import pre_trial_prompts as pp

logger = logging.getLogger(__name__)

_SECTION_NAME = "pre_trial"
_FALLBACK_SENTINEL = f"<{_SECTION_NAME}>[unavailable]</{_SECTION_NAME}>"


class _EmptyInput(BaseModel):
    pass


class PreTrialBlock(Block):
    """Audits task source for verifier/oracle/info-leakage defects and emits
    a list of ActionItems. The agent reads the task source from a local
    directory the worker downloaded for it (Read/Glob), not a sandbox."""

    output_schema = PreTrialActionItems

    def __init__(
        self, task_id: str, trial_ids: list[str], prompt_template: str
    ) -> None:
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
    def _normalize(cls, data: object) -> object:
        """Shape the model's answer into a validatable ``{"items": [...]}``.

        Both output paths route through here. They forked once already -- the
        CLI transform kept its own copy of the unwrap logic and #949's prose
        recovery reached only the other one, so the fix shipped and prod did
        not move (#959). One implementation, two callers.

        The registry prompt asks for "the structured list of action items", so
        models sometimes return a bare array instead of the envelope; wrap it
        rather than failing a valid audit on shape alone. Items that fail
        validation are dropped individually: an audit costs minutes of agent
        time and returns many findings, and rejecting the batch over one bad
        item threw every sibling finding away.
        """
        if isinstance(data, list):
            data = {"items": data}
        if not isinstance(data, dict):
            return data
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return data

        kept: list[dict] = []
        dropped: list[ValidationError] = []
        for item in items:
            try:
                kept.append(ActionItem.model_validate(item).model_dump())
            except ValidationError as exc:
                dropped.append(exc)
        if not kept:
            # Never fall through to {"items": []}: the task page renders that
            # as "no defects found", so a false all-clear would be indistinguishable
            # from a clean audit. Fail loudly instead.
            raise BlockParseError(
                f"every action item failed validation ({len(dropped)} dropped); "
                f"first: {dropped[0]}"
            )
        if dropped:
            logger.warning(
                "pre-trial: kept %d action item(s), dropped %d that failed "
                "validation; first: %s",
                len(kept),
                len(dropped),
                dropped[0],
            )
        return {**data, "items": kept}

    @classmethod
    def parse_json(cls, text: str):
        return cls._normalize(super().parse_json(text))

    def to_action_items(self, raw: str) -> dict:
        return self.parse(raw).model_dump()

    def to_action_items_from_cli(self, raw: str) -> dict:
        """``output_transform`` for the CLAUDE_CLI backend.

        The worker-local claude-code run yields stream-json events, not the
        model's bare answer, so pull the result envelope's payload out before
        validating. Feeding raw events straight to ``to_action_items`` would
        validate against ``PreTrialActionItems`` (extra keys ignored, ``items``
        defaults to empty) and silently produce zero findings on every run.
        """
        obj = self._normalize(parse_stream_json_result(raw))
        return self.filter_output(self.output_schema.model_validate(obj)).model_dump()
