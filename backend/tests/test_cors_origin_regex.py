"""Preview origins are admitted by pattern, list-only stays the default."""

from __future__ import annotations

from api.app import _get_cors_origin_regex, _get_cors_origins


def test_unset_regex_means_list_only(monkeypatch):
    monkeypatch.delenv("MODAL_APP_NAME", raising=False)
    monkeypatch.delenv("CORS_ALLOWED_ORIGIN_REGEX", raising=False)
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    assert _get_cors_origin_regex() is None
    assert _get_cors_origins() == ["http://localhost:3000", "http://127.0.0.1:3000"]


def test_preview_adds_only_its_own_stable_alias(monkeypatch):
    monkeypatch.setenv("MODAL_APP_NAME", "oddish-pr-123")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://existing.example")
    assert _get_cors_origins() == [
        "https://existing.example",
        "https://pr-123.oddish.app",
    ]


def test_production_does_not_add_preview_origins(monkeypatch):
    monkeypatch.setenv("MODAL_APP_NAME", "oddish")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://www.oddish.app")
    assert _get_cors_origins() == ["https://www.oddish.app"]


def test_regex_is_read_and_trimmed(monkeypatch):
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGIN_REGEX", r"  ^https://oddish-[a-z0-9-]+\.vercel\.app$ "
    )
    assert _get_cors_origin_regex() == r"^https://oddish-[a-z0-9-]+\.vercel\.app$"
