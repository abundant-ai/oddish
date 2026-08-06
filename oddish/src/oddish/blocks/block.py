from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from pydantic import BaseModel

logger = logging.getLogger("oddish.block")


class BlockParseError(ValueError):
    """A block's raw output could not be parsed into its output schema at all
    (as opposed to a per-element drop). Callers map this to their domain error."""


class Block:
    """Base for prompt-building blocks: turns a typed input into a prompt and a
    raw model reply into a validated output, fault-tolerant throughout.
    Subclasses supply sections(), output_schema, and (optionally) filter_output."""

    output_schema: type[BaseModel]

    # ---- prompt building ----
    def build_prompt(self) -> str:
        return "\n\n".join(self.render_section(**s) for s in self.sections())

    def sections(self) -> list[dict]:
        raise NotImplementedError

    def render_section(
        self,
        *,
        name: str,
        raw_input: dict,
        schema: type[BaseModel],
        formatter: Callable[[BaseModel], str],
        fallback: str | None = None,
    ) -> str:
        default = (
            fallback if fallback is not None else f"<{name}>[unavailable]</{name}>"
        )
        try:
            data = schema(**raw_input)
        except Exception:
            logger.exception("block section %r: input failed schema", name)
            return default
        try:
            return formatter(data)
        except Exception:
            logger.exception("block section %r: formatter raised", name)
            return default

    # ---- parsing ----
    @staticmethod
    def strip_code_fences(text: str) -> str:
        raw = text.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.lstrip().startswith("json"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw
            raw = raw.rsplit("```", 1)[0]
        return raw.strip()

    @staticmethod
    def _embedded_json(text: str) -> str | None:
        """The first JSON value embedded in prose, or None.

        ``strip_code_fences`` only fires when the fence opens the string, so a
        model that writes a sentence *before* its fenced block (or emits a bare
        object after a preamble) reaches ``json.loads`` as prose and dies on
        "line 1 column 1". Prompts already forbid the preamble; models do it
        anyway, and a whole audit is too expensive to discard over packaging.
        """
        fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
        candidates = [fence.group(1)] if fence else []
        # Ordered by where the value starts, so a bare array is not shadowed by
        # an object nested inside it.
        spans: list[tuple[int, str]] = []
        for opener, closer in (("{", "}"), ("[", "]")):
            start = text.find(opener)
            if start == -1:
                continue
            depth, in_string, escaped = 0, False, False
            for index in range(start, len(text)):
                char = text[index]
                if escaped:
                    escaped = False
                    continue
                if char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = not in_string
                elif in_string:
                    continue
                elif char == opener:
                    depth += 1
                elif char == closer:
                    depth -= 1
                    if depth == 0:
                        spans.append((start, text[start : index + 1]))
                        break
        candidates.extend(value for _, value in sorted(spans))
        for candidate in candidates:
            try:
                json.loads(candidate)
            except json.JSONDecodeError:
                continue
            return candidate
        return None

    @classmethod
    def parse_json(cls, text: str) -> Any:
        stripped = cls.strip_code_fences(text)
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            embedded = cls._embedded_json(text)
            if embedded is None:
                raise
            logger.warning("block parse: recovered JSON embedded in prose")
            return json.loads(embedded)

    def parse(self, raw: str) -> BaseModel:
        try:
            data = self.parse_json(raw)
        except Exception as e:
            logger.exception("block parse: bad JSON")
            raise BlockParseError(f"non-JSON output: {e}") from e
        if not isinstance(data, dict):
            raise BlockParseError(f"expected object, got {type(data).__name__}")
        try:
            parsed = self.output_schema(**data)
        except Exception as e:
            logger.exception("block parse: schema mismatch")
            raise BlockParseError(f"schema mismatch: {e}") from e
        return self.filter_output(parsed)

    def filter_output(self, parsed: BaseModel) -> BaseModel:
        return parsed
