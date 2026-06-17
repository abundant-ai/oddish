"""Tests for tag key/value normalization and validation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_normalize_tag_key_casefold_and_trim():
    from oddish.core.tag_naming import normalize_tag_key

    assert normalize_tag_key("  Flaky-Trial  ") == "flaky-trial"
    assert normalize_tag_key("SWE Bench v2.0") == "swe-bench-v2.0"


def test_validate_tag_key_charset():
    from oddish.core.tag_naming import validate_tag_key, TagNameError

    validate_tag_key("flaky-trial", name_max_len=64, charset_re=r"[a-z0-9._-]")

    with pytest.raises(TagNameError):
        validate_tag_key("", name_max_len=64, charset_re=r"[a-z0-9._-]")
    with pytest.raises(TagNameError):
        validate_tag_key("BadCase", name_max_len=64, charset_re=r"[a-z0-9._-]")
    with pytest.raises(TagNameError):
        validate_tag_key("contains spaces", name_max_len=64, charset_re=r"[a-z0-9._-]")
    with pytest.raises(TagNameError):
        validate_tag_key("x" * 65, name_max_len=64, charset_re=r"[a-z0-9._-]")


def test_reserved_prefix_requires_admin():
    from oddish.core.tag_naming import is_reserved_prefix

    assert is_reserved_prefix("system:critical", reserved_prefixes=["system:"]) is True
    assert is_reserved_prefix("safe-tag", reserved_prefixes=["system:"]) is False
