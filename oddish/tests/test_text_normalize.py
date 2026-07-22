"""Tests for ASCII typography normalization of task text.

Invisible characters under test (spaces, zero-width marks) are built from
``chr(codepoint)`` rather than pasted as literals: a literal no-break space is
indistinguishable from a normal space in source, so a pasted assertion could
silently test nothing. Visible glyphs (dashes, quotes, accents) are kept inline
because they are reviewable as written.
"""

from __future__ import annotations

import tomllib

from oddish.text_normalize import normalize_typography, summarize_normalization


def test_dashes_to_ascii():
    assert normalize_typography("Kolmogorov–Chentsov") == "Kolmogorov-Chentsov"
    assert normalize_typography("holes — the") == "holes -- the"
    assert normalize_typography("1‒2") == "1-2"  # figure dash
    assert normalize_typography("a−b") == "a-b"  # minus sign
    assert normalize_typography("a‑b") == "a-b"  # non-breaking hyphen


def test_quotes_to_ascii():
    assert normalize_typography("“quoted”") == '"quoted"'
    assert normalize_typography("it’s") == "it's"
    assert normalize_typography("‘x’") == "'x'"


def test_ellipsis_and_bullet():
    assert normalize_typography("wait…") == "wait..."
    assert normalize_typography("• item") == "* item"


def test_spaces_normalized_and_zero_width_removed():
    assert normalize_typography("a" + chr(0x00A0) + "b") == "a b"  # no-break
    assert normalize_typography("a" + chr(0x202F) + "b") == "a b"  # narrow no-break
    assert normalize_typography("a" + chr(0x3000) + "b") == "a b"  # ideographic
    assert normalize_typography("a" + chr(0x200B) + "b") == "ab"  # zero-width space
    assert normalize_typography(chr(0xFEFF) + "hi") == "hi"  # BOM
    assert normalize_typography("a" + chr(0x200D) + "b") == "ab"  # zero-width joiner


def test_latin_accents_transliterated():
    assert normalize_typography("Hölder") == "Holder"
    assert normalize_typography("café") == "cafe"
    assert normalize_typography("naïve résumé") == "naive resume"
    assert normalize_typography("Ñoño") == "Nono"


def test_non_latin_scripts_preserved():
    # CJK, Greek, Cyrillic, letterlike math, relations/arrows, quantifiers, and
    # emoji must survive untouched -- a task may legitimately reference them.
    for s in ["中文", "λ μ σ", "Привет", "ℝ ≤ →", "∀ε∃δ", "\U0001f680"]:
        assert normalize_typography(s) == s


def test_ascii_newlines_and_tabs_unchanged():
    s = 'line1\n\tindented = 42\nkey = "value"\n'
    assert normalize_typography(s) == s


def test_idempotent():
    s = "Kolmogorov–Chentsov — Hölder…"
    once = normalize_typography(s)
    assert once == "Kolmogorov-Chentsov -- Holder..."
    assert normalize_typography(once) == once


def test_summarize_reports_only_changed_chars():
    assert summarize_normalization("a–b—c") == {"–": "-", "—": "--"}
    assert summarize_normalization("plain ascii, 中文, ℝ") == {}


def test_normalized_task_toml_still_parses():
    raw = (
        'description = "Kolmogorov–Chentsov Hölder — mods"\n'
        "difficulty_explanation = '''feed Kolmogorov–Chentsov — done'''\n"
    )
    cleaned = normalize_typography(raw)
    assert "–" not in cleaned
    assert "—" not in cleaned
    assert "ö" not in cleaned
    parsed = tomllib.loads(cleaned)
    assert parsed["description"] == "Kolmogorov-Chentsov Holder -- mods"
    assert parsed["difficulty_explanation"] == "feed Kolmogorov-Chentsov -- done"


def test_normalize_task_config_typography_rewrites_file(tmp_path):
    from oddish.cli.api import normalize_task_config_typography

    toml = tmp_path / "task.toml"
    toml.write_text(
        'description = "Kolmogorov–Chentsov Hölder — mods"\n', encoding="utf-8"
    )
    changes = normalize_task_config_typography(tmp_path)

    assert changes == {"–": "-", "ö": "o", "—": "--"}
    text = toml.read_text(encoding="utf-8")
    assert tomllib.loads(text)["description"] == "Kolmogorov-Chentsov Holder -- mods"


def test_normalize_task_config_typography_noop_leaves_file_untouched(tmp_path):
    from oddish.cli.api import normalize_task_config_typography

    toml = tmp_path / "task.toml"
    original = 'description = "plain ascii"\nkeep = "中文 ℝ ≤"\n'
    toml.write_text(original, encoding="utf-8")

    assert normalize_task_config_typography(tmp_path) == {}
    assert toml.read_text(encoding="utf-8") == original


def test_normalize_task_config_typography_missing_file(tmp_path):
    from oddish.cli.api import normalize_task_config_typography

    assert normalize_task_config_typography(tmp_path) == {}
