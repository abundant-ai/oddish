from __future__ import annotations

import importlib
from pathlib import Path
import sys

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.cli import app


def test_run_help_includes_manifest_alias() -> None:
    result = CliRunner().invoke(app, ["run", "--help"])

    assert result.exit_code == 0
    assert "--manifest" in result.output
    assert "--config" in result.output
    assert "manifest" in result.output


def test_run_manifest_alias_loads_sweep_config(monkeypatch, tmp_path: Path) -> None:
    run_module = importlib.import_module("oddish.cli.run")
    manifest_path = tmp_path / "experiment.yaml"
    manifest_path.write_text("agents: []\n", encoding="utf-8")
    task_path = tmp_path / "task"
    task_path.mkdir()
    loaded_paths: list[Path] = []

    def fake_load_sweep_config(path: Path) -> dict:
        loaded_paths.append(path)
        return {
            "agents": [{"agent": "codex", "model": "openai/gpt-5.2", "n_trials": 1}],
            "path": str(task_path),
            "experiment_id": "manifest-exp",
        }

    def fake_submit_sweep(**kwargs: object) -> dict:
        assert kwargs["experiment_id"] == "manifest-exp"
        assert kwargs["configs"] == [
            {"agent": "codex", "model": "openai/gpt-5.2", "n_trials": 1}
        ]
        return {
            "id": "task_123",
            "trials_count": 1,
            "providers": {"openai": {}},
            "experiment_id": "exp_123",
            "experiment_name": "manifest-exp",
        }

    monkeypatch.setattr(run_module, "require_api_key", lambda _api_url: None)
    monkeypatch.setattr(run_module, "is_modal_api_url", lambda _api_url: False)
    monkeypatch.setattr(run_module, "load_sweep_config", fake_load_sweep_config)
    monkeypatch.setattr(
        run_module,
        "resolve_local_task_paths",
        lambda **_kwargs: [task_path],
    )
    monkeypatch.setattr(
        run_module,
        "upload_tasks_with_progress",
        lambda *_args, **_kwargs: [
            {"task_id": "task_123", "existing_task": False, "content_hash": "hash"}
        ],
    )
    monkeypatch.setattr(run_module, "submit_sweep", fake_submit_sweep)
    monkeypatch.setattr(run_module, "get_dashboard_url", lambda _api_url: "")

    result = CliRunner().invoke(
        app,
        [
            "run",
            "--manifest",
            str(manifest_path),
            "--background",
            "--api",
            "http://api.example.test",
        ],
    )

    assert result.exit_code == 0
    assert loaded_paths == [manifest_path]
