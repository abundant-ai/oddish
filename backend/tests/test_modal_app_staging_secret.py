"""The per-app DB-override secret must attach for oddish-pr-* AND oddish-staging."""

import importlib
import sys

import pytest


def _load_modal_app(monkeypatch, app_name: str):
    recorded: list[str] = []
    import modal

    real_from_name = modal.Secret.from_name

    def recording_from_name(name, *args, **kwargs):
        recorded.append(name)
        return real_from_name(name, *args, **kwargs)

    monkeypatch.setattr(modal.Secret, "from_name", staticmethod(recording_from_name))
    monkeypatch.setenv("MODAL_APP_NAME", app_name)
    monkeypatch.setenv("MODAL_ENVIRONMENT", "staging")
    sys.modules.pop("modal_app", None)
    importlib.import_module("modal_app")
    return recorded


@pytest.mark.parametrize(
    ("app_name", "expects_override"),
    [
        ("oddish", False),
        ("oddish-pr-123", True),
        ("oddish-staging", True),
    ],
)
def test_db_override_secret_attachment(monkeypatch, app_name, expects_override):
    recorded = _load_modal_app(monkeypatch, app_name)
    assert (f"{app_name}-db" in recorded) is expects_override
