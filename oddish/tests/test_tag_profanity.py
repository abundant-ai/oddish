"""Tests for tag_profanity.

We rely on the open-source ``better-profanity`` package. To avoid a hard
dependency in the test sandbox we stub the import in tests; production
adds ``better-profanity`` to backend deps.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_check_tag_text_off_mode_always_allows(monkeypatch):
    from oddish.core import tag_profanity

    policy = SimpleNamespace(
        profanity_mode="OFF",
        profanity_allowlist=[],
        profanity_denylist=[],
    )
    res = tag_profanity.check_tag_text("offensive-thing", policy)
    assert res.allowed is True
    assert res.hits == []


def test_check_tag_text_enforce_with_denylist_rejects(monkeypatch):
    from oddish.core import tag_profanity

    class _FakeFilter:
        def add_censor_words(self, words):
            self._words = words

        def load_censor_words(self, custom_words=None):
            self._words = custom_words or []

        def contains_profanity(self, text):
            return any(w in text for w in self._words)

    monkeypatch.setattr(tag_profanity, "_build_filter", lambda policy: _FakeFilter())

    policy = SimpleNamespace(
        profanity_mode="ENFORCE",
        profanity_allowlist=[],
        profanity_denylist=["zzz-bad"],
    )
    res = tag_profanity.check_tag_text("contains-zzz-bad-here", policy)
    assert res.allowed is False
    assert "zzz-bad" in res.hits[0]


def test_check_tag_text_report_mode_allows_but_flags(monkeypatch):
    from oddish.core import tag_profanity

    class _FakeFilter:
        def add_censor_words(self, words):
            self._words = words

        def load_censor_words(self, custom_words=None):
            self._words = custom_words or []

        def contains_profanity(self, text):
            return True

    monkeypatch.setattr(tag_profanity, "_build_filter", lambda policy: _FakeFilter())

    policy = SimpleNamespace(
        profanity_mode="REPORT",
        profanity_allowlist=[],
        profanity_denylist=["x"],
    )
    res = tag_profanity.check_tag_text("anything", policy)
    assert res.allowed is True
    assert res.hits  # flagged for backstop admin queue
