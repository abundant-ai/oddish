"""Preview origins are admitted by pattern, list-only stays the default."""

from __future__ import annotations

from api.app import _get_cors_origin_regex, _get_cors_origins


def test_unset_regex_means_list_only(monkeypatch):
    monkeypatch.delenv("CORS_ALLOWED_ORIGIN_REGEX", raising=False)
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    assert _get_cors_origin_regex() is None
    assert _get_cors_origins() == ["http://localhost:3000", "http://127.0.0.1:3000"]


def test_regex_is_read_and_trimmed(monkeypatch):
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGIN_REGEX", r"  ^https://oddish-[a-z0-9-]+\.vercel\.app$ "
    )
    assert _get_cors_origin_regex() == r"^https://oddish-[a-z0-9-]+\.vercel\.app$"
