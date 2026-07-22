"""Tests for oddish.blocks.block.Block."""
from __future__ import annotations

import logging

import pytest
from pydantic import BaseModel

from oddish.blocks.block import Block, BlockParseError


class _In(BaseModel):
    name: str


class _Out(BaseModel):
    value: str = ""
    items: list[dict] = []


class _Demo(Block):
    output_schema = _Out

    def sections(self):
        return [
            {"name": "greeting", "raw_input": {"name": "world"}, "schema": _In,
             "formatter": lambda d: f"<greeting>hi {d.name}</greeting>"},
        ]


def test_build_prompt_renders_sections():
    assert _Demo().build_prompt() == "<greeting>hi world</greeting>"


def test_render_section_falls_back_and_logs_on_bad_input(caplog):
    with caplog.at_level(logging.ERROR):
        out = _Demo().render_section(
            name="greeting", raw_input={}, schema=_In,
            formatter=lambda d: f"hi {d.name}",
        )
    assert out == "<greeting>[unavailable]</greeting>"
    assert "greeting" in caplog.text


def test_render_section_falls_back_and_logs_on_formatter_error(caplog):
    def boom(_d):
        raise RuntimeError("nope")

    with caplog.at_level(logging.ERROR):
        out = _Demo().render_section(
            name="x", raw_input={"name": "a"}, schema=_In, formatter=boom,
            fallback="FALLBACK",
        )
    assert out == "FALLBACK"
    assert "formatter raised" in caplog.text


def test_parse_valid_returns_output_schema():
    out = _Demo().parse('{"value": "ok", "items": [{"a": 1}]}')
    assert isinstance(out, _Out)
    assert out.value == "ok"


def test_parse_strips_code_fences():
    out = _Demo().parse('```json\n{"value": "fenced"}\n```')
    assert out.value == "fenced"


def test_parse_raises_blockparseerror_on_bad_json():
    with pytest.raises(BlockParseError):
        _Demo().parse("not json")


def test_parse_raises_blockparseerror_on_non_object():
    with pytest.raises(BlockParseError):
        _Demo().parse("[1, 2, 3]")


def test_filter_output_hook_is_applied():
    class _Filtered(_Demo):
        def filter_output(self, parsed):
            parsed.value = parsed.value.upper()
            return parsed

    assert _Filtered().parse('{"value": "ok"}').value == "OK"
