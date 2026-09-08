"""Deployment limits must survive container import and older named secrets."""

import importlib

import modal_app


def test_deployed_worker_limits_survive_runtime_import(monkeypatch):
    from oddish.config import Settings

    captured = []
    original = modal_app.modal.Secret.from_dict

    def capture(values, *args, **kwargs):
        captured.append(dict(values))
        return original(values, *args, **kwargs)

    monkeypatch.setattr(modal_app.modal.Secret, "from_dict", capture)
    requested = {
        "ODDISH_MODAL_MAX_WORKERS_PER_POLL": "300",
        "ODDISH_MODAL_WORKER_MAX_CONTAINERS": "300",
        "ODDISH_DEFAULT_MODEL_CONCURRENCY": "300",
        "ODDISH_MODEL_CONCURRENCY_OVERRIDES": "{}",
    }
    for name, value in requested.items():
        monkeypatch.setenv(name, value)
    importlib.reload(modal_app)
    try:
        authoritative = captured[-1]
        assert all(authoritative[name] == value for name, value in requested.items())
        assert all(
            modal_app.ENV_VARS[name] == value for name, value in requested.items()
        )
        dependency_count = len(modal_app.runtime_secrets)

        # A fresh container gets image env, then old provider-secret values,
        # then the final deployment-owned secret. It has no deploy shell.
        for name in requested:
            monkeypatch.delenv(name)
        for name, value in authoritative.items():
            monkeypatch.setenv(name, value)
        importlib.reload(modal_app)
        assert modal_app.MAX_WORKERS_PER_POLL == 300
        assert modal_app.WORKER_MAX_CONTAINERS == 300
        assert len(modal_app.runtime_secrets) == dependency_count
        settings = Settings(_env_file=None)
        assert settings.get_model_concurrency("openai/test-model-10") == 300
        assert settings.get_model_concurrency("nop_oracle") >= 300
    finally:
        monkeypatch.undo()
        importlib.reload(modal_app)
