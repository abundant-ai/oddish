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
    """Turn typed input into a prompt and a raw reply into validated output.

    Subclasses supply ``sections()``, ``output_schema``, and optionally
    ``filter_output()``. A provider-constrained block sets
    ``strict_json_output`` so transport violations surface instead of entering
    the recovery path used by free-text and CLI backends.
    """

    output_schema: type[BaseModel]
    strict_json_output: bool = False

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
    def _fence_body(text: str) -> str | None:
        """The body of the first ``` fence, or None if the reply has none.

        Separate from ``strip_code_fences`` because that one only fires when the
        fence opens the string; this finds one that follows a preamble.
        """
        fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
        return fence.group(1) if fence else None

    @staticmethod
    def _embedded_json(text: str, expect: type | None = None) -> str | None:
        """The first substantive JSON value embedded in prose, or None.

        Reached only for a reply with no fence at all -- ``parse_json`` resolves
        a fenced answer itself, because a fence outranks anything scanned out of
        the surrounding prose. This is the bare-object case: a model that emits
        its payload after a preamble reaches ``json.loads`` as prose and dies on
        "line 1 column 1". Prompts already forbid the preamble; models do it
        anyway, and a whole audit is too expensive to discard over packaging.

        ``expect`` skips candidates of the wrong type, and anything nested
        inside an already-decoded value is skipped with it, so a bare array of
        the wrong shape cannot be mined for the object the caller asked for.
        """
        decoder = json.JSONDecoder(strict=False)
        empty: str | None = None

        # Every opener is a candidate, in position order: audit prose quotes
        # code constantly, so a brace before the payload is the common case.
        # Scanning only the first one loses the audit to `{task_id}` and
        # (worse) silently returns `{}` for "the runner returns {} on timeout".
        # Ordering by position also keeps a bare array from being shadowed by
        # an object nested inside it.
        consumed_until = 0
        for start, char in enumerate(text):
            if char not in "{[":
                continue
            # An opener inside a value that already decoded is a fragment of it,
            # not a value in its own right. Mining those turns a well-formed
            # reply of the wrong shape into whichever inner object matches --
            # a summary persisted as SUCCESS with every field empty.
            if start < consumed_until:
                continue
            try:
                value, end = decoder.raw_decode(text[start:])
            except ValueError:
                # A value that failed to decode still consumes its brackets.
                # Its children are fragments of a broken payload, not answers:
                # mining one returns a lone `highlights` entry as the whole
                # summary, which persists as SUCCESS saying nothing. A
                # `{task_id}` placeholder closes immediately, so this skips
                # only itself and the real payload after it still wins.
                # An unbalanced one cannot be bounded, and the usual reason is
                # a stray quote inside it -- so everything after is suspect too.
                extent = Block._balanced_extent(text, start)
                consumed_until = extent if extent is not None else len(text)
                continue
            consumed_until = start + end
            if expect is not None and not isinstance(value, expect):
                continue
            if value:
                return text[start : start + end]
            # An empty container is far more likely prose punctuation than the
            # audit, so keep looking -- but return it if nothing better exists.
            if empty is None:
                empty = text[start : start + end]
        return empty

    @staticmethod
    def _balanced_extent(text: str, start: int) -> int | None:
        """Index just past the bracket matching ``text[start]``, or None.

        Bracket counting, not JSON parsing, so it also measures a value the
        decoder rejected -- which is the point: it bounds a broken object so
        its children are not mistaken for values of their own.
        """
        opener = text[start]
        closer = "}" if opener == "{" else "]"
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(text)):
            char = text[index]
            if escaped:
                escaped = False
            elif char == "\\":
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
                    return index + 1
        return None

    @staticmethod
    def _repair_json(
        text: str,
        expect: type | None = None,
        *,
        allow_trailing: bool = False,
        require_repair: bool = False,
    ) -> Any:
        """Re-insert dropped punctuation, one decode error at a time, or None.

        Reached only when the text already failed to parse as-is, so it cannot
        regress a working reply. Each pass fixes exactly the character the
        decoder names at the position it names; anything the decoder reports
        differently is left alone rather than guessed at. Replayed over 250
        failed prod blocks it recovered 38 with zero malformed results.

        ``allow_trailing`` accepts prose after the value and ``require_repair``
        declines a value that needed no mending, both for the call that anchors
        on an opener inside a larger reply: there, a value that parses as-is is
        just prose punctuation, and ``_embedded_json`` -- which knows to prefer
        a substantive value over an empty one -- owns that case.
        """
        decoder = json.JSONDecoder(strict=False)
        repairs = 0
        for _ in range(_MAX_JSON_REPAIRS):
            try:
                value, end = decoder.raw_decode(text)
            except json.JSONDecodeError as e:
                for prefix, char in _JSON_REPAIRS.items():
                    if e.msg.startswith(prefix):
                        text = text[: e.pos] + char + text[e.pos :]
                        repairs += 1
                        break
                else:
                    return None
                continue
            if require_repair and not repairs:
                return None
            if not allow_trailing and text[end:].strip():
                return None
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

        Repair is attempted before prose recovery because it works on the whole
        reply: a top-level object missing one delimiter should be mended, not
        abandoned for whichever fragment nested inside it happens to parse. That
        fallback yields a summary persisted as SUCCESS with every field empty --
        worse than surfacing the parse error.
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
        repaired = cls._repair_json(stripped, expect)
        if repaired is not None:
            logger.warning("block parse: recovered JSON after punctuation repair")
            return repaired
        # A fence is the model delimiting its own answer, so it settles the reply
        # before anything is scanned or mended out of the surrounding prose --
        # otherwise an example the model quoted while explaining itself outranks
        # what it actually fenced. ``strip_code_fences`` above only fires when
        # the fence opens the string, so one that follows a preamble lands here.
        body = cls._fence_body(text)
        if body is not None:
            try:
                value = json.loads(body, strict=False)
            except json.JSONDecodeError as fence_error:
                mended = cls._repair_json(body, expect, allow_trailing=True)
                if mended is not None:
                    logger.warning("block parse: recovered fenced JSON after repair")
                    return mended
                # The fence bounds the answer, so a body that will not mend means
                # the answer is broken -- not that it is somewhere else in the
                # prose. Scanning on would return a fragment of this very object.
                raise fence_error
            # Returned even when it is the wrong type, so the caller reports the
            # mismatch; falling through would mine an example from the preamble.
            return value
        # Same repair, anchored on the first opener: a prose-wrapped object
        # missing one delimiter never reaches the branch above, because the
        # leading prose fails at "Expecting value" long before the real defect.
        # Without this the scan below decodes straight past the broken value
        # and returns a fragment nested inside it.
        opener = min(
            (i for i in (text.find("{"), text.find("[")) if i != -1), default=-1
        )
        if opener != -1:
            repaired = cls._repair_json(
                text[opener:], expect, allow_trailing=True, require_repair=True
            )
            if repaired is not None:
                logger.warning("block parse: recovered JSON after punctuation repair")
                return repaired
        embedded = cls._embedded_json(text, expect)
        if embedded is not None:
            logger.warning("block parse: recovered JSON embedded in prose")
            return json.loads(embedded, strict=False)
        if decode_error is not None:
            raise decode_error
        # Parsed cleanly but as the wrong type: hand it back so the caller
        # reports the type mismatch rather than a bogus decode failure.
        return value

    def parse(self, raw: str) -> BaseModel:
        if self.strict_json_output:
            try:
                parsed = self.output_schema.model_validate_json(raw)
            except Exception as e:
                logger.exception("block parse: structured output mismatch")
                raise BlockParseError(f"structured output mismatch: {e}") from e
            return self.filter_output(parsed)

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
