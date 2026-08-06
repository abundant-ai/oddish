from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from pydantic import BaseModel

logger = logging.getLogger("oddish.block")

# Punctuation a model drops when it closes a long string value early and runs
# straight into the next key. Each entry maps a ``JSONDecodeError.msg`` prefix
# to the character that belongs at ``JSONDecodeError.pos``.
_JSON_REPAIRS = {
    "Expecting ',' delimiter": ",",
    "Expecting ':' delimiter": ":",
    "Expecting property name enclosed": '"',
}
_MAX_JSON_REPAIRS = 8


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
    def _embedded_json(text: str, expect: type | None = None) -> str | None:
        """The first JSON value embedded in prose, or None.

        ``strip_code_fences`` only fires when the fence opens the string, so a
        model that writes a sentence *before* its fenced block (or emits a bare
        object after a preamble) reaches ``json.loads`` as prose and dies on
        "line 1 column 1". Prompts already forbid the preamble; models do it
        anyway, and a whole audit is too expensive to discard over packaging.

        ``expect`` restricts recovery to candidates of that type. Without it a
        broken top-level object degrades into whichever nested array happens to
        parse, so the caller sees a type error instead of the real defect.
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
        spans.sort()
        if expect is not None and spans:
            # Only the outermost value is a legitimate recovery. Digging into it
            # turns a broken object into whichever nested fragment happens to
            # match the expected type -- a summary that persists as SUCCESS with
            # every field empty, which is worse than surfacing the parse error.
            spans = [s for s in spans if s[0] == spans[0][0]]
        candidates.extend(value for _, value in spans)
        for candidate in candidates:
            try:
                value = json.loads(candidate, strict=False)
            except json.JSONDecodeError:
                continue
            if expect is not None and not isinstance(value, expect):
                continue
            return candidate
        return None

    @staticmethod
    def _repair_json(text: str, expect: type | None = None) -> Any:
        """Re-insert dropped punctuation, one decode error at a time, or None.

        Last resort, reached only when the text already failed to parse, so it
        cannot regress a working reply. Each pass fixes exactly the character
        the decoder names at the position it names; anything the decoder
        reports differently is left alone rather than guessed at. Replayed over
        250 failed prod blocks it recovered 38 with zero malformed results.
        """
        for _ in range(_MAX_JSON_REPAIRS):
            try:
                value = json.loads(text, strict=False)
            except json.JSONDecodeError as e:
                for prefix, char in _JSON_REPAIRS.items():
                    if e.msg.startswith(prefix):
                        text = text[: e.pos] + char + text[e.pos :]
                        break
                else:
                    return None
                continue
            if expect is not None and not isinstance(value, expect):
                return None
            return value
        return None

    @classmethod
    def parse_json(cls, text: str, *, expect: type | None = None) -> Any:
        """Parse the model's reply, tolerating the packaging models actually emit.

        ``strict=False`` permits literal control characters inside string
        values. Models write multi-paragraph prose into one JSON string and
        leave the newlines unescaped, which strict ``json.loads`` rejects. It is
        the single largest cause of discarded output: replaying 250 failed
        trajectory-summary blocks from prod (2026-08-05), leniency alone
        recovered 101 and the repair pass below took the total to 139.
        """
        stripped = cls.strip_code_fences(text)
        decode_error: json.JSONDecodeError | None = None
        try:
            value = json.loads(stripped, strict=False)
        except json.JSONDecodeError as e:
            decode_error = e
        else:
            if expect is None or isinstance(value, expect):
                return value
        embedded = cls._embedded_json(text, expect)
        if embedded is not None:
            logger.warning("block parse: recovered JSON embedded in prose")
            return json.loads(embedded, strict=False)
        repaired = cls._repair_json(stripped, expect)
        if repaired is not None:
            logger.warning("block parse: recovered JSON after punctuation repair")
            return repaired
        if decode_error is not None:
            raise decode_error
        # Parsed cleanly but as the wrong type: hand it back so the caller
        # reports the type mismatch rather than a bogus decode failure.
        return value

    def parse(self, raw: str) -> BaseModel:
        try:
            data = self.parse_json(raw, expect=dict)
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
