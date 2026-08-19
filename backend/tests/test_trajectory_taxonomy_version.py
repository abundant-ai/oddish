"""Freshness must track the label vocabulary, not just the response shape."""
from __future__ import annotations

import pytest

from api.services import summarize_trajectory as st
from api.services.blocks.analyzer.trajectory import trajectory_prompts as tp


def _fresh(**overrides):
    base = {
        "schema_version": st.SCHEMA_VERSION,
        "taxonomy_version": st.taxonomy_version(),
    }
    base.update(overrides)
    return base


def test_fresh_summary_accepts_current_schema_and_taxonomy():
    assert st.is_fresh_summary(_fresh()) is True


def test_stale_when_taxonomy_differs_even_at_current_schema():
    """The regression this exists for: a retired label served forever.

    Production summaries still carry `thinking_diagnose`, written under an
    older vocabulary at a schema version that never changed.
    """
    assert st.is_fresh_summary(_fresh(taxonomy_version="deadbeef1234")) is False


def test_stale_when_taxonomy_version_absent():
    summary = {"schema_version": st.SCHEMA_VERSION}
    assert st.is_fresh_summary(summary) is False


def test_stale_when_schema_differs():
    assert st.is_fresh_summary(_fresh(schema_version="1")) is False


@pytest.mark.parametrize("value", [None, "x", 3, [], {"schema_version": None}])
def test_non_dict_and_junk_are_stale(value):
    assert st.is_fresh_summary(value) is False


def test_fingerprint_is_stable_across_calls():
    assert tp.taxonomy_fingerprint() == tp.taxonomy_fingerprint()


def test_fingerprint_changes_when_a_description_changes(monkeypatch):
    before = tp.taxonomy_fingerprint()
    patched = dict(tp.TAXONOMY_DESCRIPTIONS)
    patched["debugging"] = "something materially different."
    monkeypatch.setattr(tp, "TAXONOMY_DESCRIPTIONS", patched)
    assert tp.taxonomy_fingerprint() != before


def test_fingerprint_changes_when_precedence_changes(monkeypatch):
    before = tp.taxonomy_fingerprint()
    monkeypatch.setattr(tp, "TAXONOMY_PRECEDENCE", "different tie-break rules")
    assert tp.taxonomy_fingerprint() != before


def test_fingerprint_ignores_unrelated_prompt_prose():
    """A typo fix in the template must not invalidate ~72k paid summaries."""
    before = tp.taxonomy_fingerprint()
    tp.instructions_section(
        "totally different wrapper prose {{taxonomy}}",
        ["reading_files"],
        ["implementing"],
    )
    assert tp.taxonomy_fingerprint() == before


def test_precedence_rules_reach_the_rendered_prompt():
    rendered = tp.render_taxonomy(["reading_files"], ["debugging"])
    assert "only when no specific failure is being chased" in rendered


def test_axes_reach_the_rendered_instructions():
    out = tp.instructions_section("{{taxonomy}}", ["reading_files"], ["debugging"])
    assert "`action`" in out and "`purpose`" in out
    assert "diagnose" in out
