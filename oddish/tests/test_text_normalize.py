"""Tests for ASCII typography normalization of task text.

Invisible characters under test (spaces, zero-width marks, joiners) are built
from ``chr(codepoint)`` rather than pasted as literals: a literal no-break space
is indistinguishable from a normal space in source, so a pasted assertion could
silently test nothing. Visible glyphs (dashes, quotes, accents) are kept inline
because they are reviewable as written.
"""

from __future__ import annotations

import tomllib

from oddish.text_normalize import normalize_typography, summarize_normalization

# --- pure normalize_typography ------------------------------------------------


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


def test_spaces_normalized_and_accidental_invisibles_removed():
    assert normalize_typography("a" + chr(0x00A0) + "b") == "a b"  # no-break space
    assert normalize_typography("a" + chr(0x202F) + "b") == "a b"  # narrow no-break
    assert normalize_typography("a" + chr(0x3000) + "b") == "a b"  # ideographic
    assert normalize_typography("a" + chr(0x200B) + "b") == "ab"  # zero-width space
    assert normalize_typography(chr(0xFEFF) + "hi") == "hi"  # BOM
    assert normalize_typography("soft" + chr(0x00AD) + "hyphen") == "softhyphen"


def test_join_controls_are_preserved():
    # ZWJ emoji sequences and ZWNJ in real scripts are structural, not noise.
    family = "\U0001f468" + chr(0x200D) + "\U0001f469" + chr(0x200D) + "\U0001f467"
    assert normalize_typography(family) == family
    persian = "می" + chr(0x200C) + "خواهم"  # contains ZWNJ (U+200C)
    assert normalize_typography(persian) == persian


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


# --- structural normalize_task_config_typography ------------------------------


def _write(tmp_path, text):
    (tmp_path / "task.toml").write_text(text, encoding="utf-8")
    return tmp_path / "task.toml"


def test_hook_normalizes_metadata_and_preserves_layout(tmp_path):
    from oddish.cli.api import normalize_task_config_typography

    toml = _write(
        tmp_path,
        "[metadata]\n"
        "# keep this comment\n"
        'tags = ["a–b", "plain"]\n'
        "description = '''Kolmogorov–Chentsov Hölder — mods'''\n",
    )
    changes = normalize_task_config_typography(tmp_path)

    assert changes == {"–": "-", "ö": "o", "—": "--"}
    out = toml.read_text(encoding="utf-8")
    assert "# keep this comment" in out
    parsed = tomllib.loads(out)
    assert parsed["metadata"]["description"] == "Kolmogorov-Chentsov Holder -- mods"
    assert parsed["metadata"]["tags"] == ["a-b", "plain"]


def test_hook_curly_quotes_stay_valid_toml(tmp_path):
    # Regression: raw-text replacement produced `description = "He said "hi""`,
    # which is invalid TOML. Structural editing re-escapes correctly.
    from oddish.cli.api import normalize_task_config_typography

    toml = _write(tmp_path, '[metadata]\ndescription = "He said “hi”"\n')
    normalize_task_config_typography(tmp_path)
    parsed = tomllib.loads(toml.read_text(encoding="utf-8"))
    assert parsed["metadata"]["description"] == 'He said "hi"'


def test_hook_leaves_runtime_fields_untouched(tmp_path):
    # Regression: normalizing execution-relevant values corrupted them.
    from oddish.cli.api import normalize_task_config_typography

    toml = _write(
        tmp_path,
        'artifacts = ["résumé.pdf"]\n'
        "\n"
        "[metadata]\n"
        'description = "café — x"\n'
        "\n"
        "[environment.env]\n"
        'EXPECTED = "café"\n'
        'allowed = "a—b"\n',
    )
    changes = normalize_task_config_typography(tmp_path)
    parsed = tomllib.loads(toml.read_text(encoding="utf-8"))

    assert changes == {"é": "e", "—": "--"}  # only from [metadata].description
    assert parsed["artifacts"] == ["résumé.pdf"]
    assert parsed["environment"]["env"]["EXPECTED"] == "café"
    assert parsed["environment"]["env"]["allowed"] == "a—b"
    assert parsed["metadata"]["description"] == "cafe -- x"


def test_hook_does_not_change_content_hash(tmp_path):
    from oddish.cli.api import (
        compute_task_content_hash,
        normalize_task_config_typography,
    )

    # A runnable-enough task dir: content hash reads only runtime fields.
    _write(
        tmp_path,
        "[metadata]\n"
        'description = "Kolmogorov–Chentsov Hölder"\n'
        "\n"
        "[environment]\n"
        "build_timeout_sec = 1800.0\n"
        "\n"
        "[agent]\n"
        "timeout_sec = 18000.0\n"
        "\n"
        "[verifier]\n"
        "timeout_sec = 1500.0\n",
    )
    before = compute_task_content_hash(tmp_path)
    changes = normalize_task_config_typography(tmp_path)
    after = compute_task_content_hash(tmp_path)

    assert changes  # metadata really did change
    assert before == after


def test_hook_noop_when_clean(tmp_path):
    from oddish.cli.api import normalize_task_config_typography

    original = '[metadata]\ndescription = "plain ascii"\nkeep = "中文 ℝ ≤"\n'
    toml = _write(tmp_path, original)
    assert normalize_task_config_typography(tmp_path) == {}
    assert toml.read_text(encoding="utf-8") == original


def test_hook_noop_when_no_metadata_table(tmp_path):
    from oddish.cli.api import normalize_task_config_typography

    original = 'version = "1.0"\n\n[environment]\nx = "a—b"\n'
    toml = _write(tmp_path, original)
    assert normalize_task_config_typography(tmp_path) == {}
    assert toml.read_text(encoding="utf-8") == original


def test_hook_missing_file(tmp_path):
    from oddish.cli.api import normalize_task_config_typography

    assert normalize_task_config_typography(tmp_path) == {}


def test_hook_skips_symlink(tmp_path):
    from oddish.cli.api import normalize_task_config_typography

    target = tmp_path / "real.toml"
    target.write_text('[metadata]\ndescription = "a—b"\n', encoding="utf-8")
    link = tmp_path / "task.toml"
    link.symlink_to(target)

    assert normalize_task_config_typography(tmp_path) == {}
    # The symlink target must not be rewritten through the link.
    assert target.read_text(encoding="utf-8") == '[metadata]\ndescription = "a—b"\n'


def test_hook_ignores_non_utf8(tmp_path):
    from oddish.cli.api import normalize_task_config_typography

    raw = b'[metadata]\ndescription = "\xff\xfe not utf-8"\n'
    (tmp_path / "task.toml").write_bytes(raw)
    assert normalize_task_config_typography(tmp_path) == {}
    assert (tmp_path / "task.toml").read_bytes() == raw


def test_hook_ignores_unparseable_toml(tmp_path):
    from oddish.cli.api import normalize_task_config_typography

    raw = "this is [not valid toml —\n"
    (tmp_path / "task.toml").write_text(raw, encoding="utf-8")
    assert normalize_task_config_typography(tmp_path) == {}
    assert (tmp_path / "task.toml").read_text(encoding="utf-8") == raw
