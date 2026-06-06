"""Tests for experiment spec capture (manifest + CLI command).

These cover the CLI wiring that records the raw sweep config and the verbatim
``oddish run`` invocation on an experiment for replicate/track, and the
guarantee that neither field is exposed on any public/share response model.
"""

from __future__ import annotations

from oddish.cli import api as cli_api
from oddish.cli.api import submit_sweep
from oddish.schemas import (
    PublicExperimentListItem,
    PublicExperimentResponse,
    TaskStatusResponse,
    TaskSweepSubmission,
)


def test_task_sweep_submission_accepts_manifest_and_command():
    submission = TaskSweepSubmission(
        task_id="task-123",
        configs=[{"agent": "codex", "model": "gpt-5", "n_trials": 1}],
        manifest="agents:\n  - name: codex\n    model_name: gpt-5\n",
        command="oddish run tasks -c sweep.yaml --experiment exp",
    )
    assert submission.manifest is not None
    assert submission.manifest.startswith("agents:")
    assert submission.command == "oddish run tasks -c sweep.yaml --experiment exp"


def test_task_sweep_submission_manifest_and_command_default_to_none():
    submission = TaskSweepSubmission(
        task_id="task-123",
        configs=[{"agent": "codex", "model": "gpt-5", "n_trials": 1}],
    )
    assert submission.manifest is None
    assert submission.command is None


def _patch_httpx_capture(monkeypatch, captured: list[dict]) -> None:
    class _Response:
        status_code = 200
        text = "{}"

        def json(self):
            return {"id": "task-123", "trials_count": 1}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, _url, json):
            captured.append(json)
            return _Response()

    monkeypatch.setattr(cli_api, "get_auth_headers", lambda: {})
    monkeypatch.setattr(cli_api.httpx, "Client", _Client)


def test_submit_sweep_includes_manifest_and_command_only_when_set(monkeypatch):
    captured: list[dict] = []
    _patch_httpx_capture(monkeypatch, captured)

    base_kwargs = {
        "api_url": "https://oddish.example",
        "task_id": "task-123",
        "configs": [{"agent": "codex", "model": "gpt-5", "n_trials": 1}],
        "environment": None,
        "user": None,
        "priority": "low",
        "experiment_id": "exp-123",
    }
    submit_sweep(**base_kwargs)
    submit_sweep(
        **base_kwargs,
        manifest="agents:\n  - name: codex\n",
        command="oddish run tasks -c sweep.yaml --experiment exp-123",
    )

    # Omitted entirely when not provided (e.g. flag-only runs).
    assert "manifest" not in captured[0]
    assert "command" not in captured[0]
    # Forwarded verbatim when provided.
    assert captured[1]["manifest"].startswith("agents:")
    assert (
        captured[1]["command"]
        == "oddish run tasks -c sweep.yaml --experiment exp-123"
    )


def test_public_response_models_never_expose_experiment_spec():
    """Hidden-on-public guarantee: the spec must not be a field on any
    public/share response model, so a public share view can never serialize it.
    """
    for model in (
        PublicExperimentResponse,
        PublicExperimentListItem,
        TaskStatusResponse,
    ):
        assert "manifest" not in model.model_fields, model.__name__
        assert "command" not in model.model_fields, model.__name__
