import importlib

from oddish.config import settings


def test_modal_concurrency_defaults_ignore_runtime_environment(monkeypatch):
    monkeypatch.setenv("ODDISH_DEFAULT_MODEL_CONCURRENCY", "1")
    monkeypatch.setenv("ODDISH_MODAL_NOP_ORACLE_CONCURRENCY", "2")
    monkeypatch.setenv(
        "ODDISH_MODEL_CONCURRENCY_OVERRIDES",
        '{"anthropic/claude-sonnet-5": 3}',
    )

    import modal_app

    modal_app = importlib.reload(modal_app)

    assert modal_app.MODEL_CONCURRENCY_DEFAULT == 48
    assert modal_app.NOP_ORACLE_CONCURRENCY == 256
    assert modal_app.MODEL_CONCURRENCY_OVERRIDES["anthropic/claude-sonnet-5"] == 256
    assert settings.get_model_concurrency("anthropic/claude-sonnet-5") == 256
    assert "ODDISH_DEFAULT_MODEL_CONCURRENCY" not in modal_app.ENV_VARS
    assert "ODDISH_MODEL_CONCURRENCY_OVERRIDES" not in modal_app.ENV_VARS
    assert "ODDISH_NOP_ORACLE_CONCURRENCY" not in modal_app.ENV_VARS
