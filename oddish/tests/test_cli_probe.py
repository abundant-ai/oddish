from unittest.mock import patch

from typer.testing import CliRunner

from oddish.cli import app


def _set_env(monkeypatch):
    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    monkeypatch.setenv("ODDISH_API_URL", "https://api.example.test")


def test_probe_without_subcommand_still_runs_probe(monkeypatch):
    _set_env(monkeypatch)
    captured = {}

    def _fake_submit_sweep(*, api_url, task_id, extra_instructions, **kwargs):
        captured["task_id"] = task_id
        captured["extra_instructions"] = extra_instructions
        return {"id": task_id, "new_trial_ids": ["tr1"], "trials_count": 1}

    with patch("oddish.cli.probe.submit_sweep", _fake_submit_sweep):
        result = CliRunner().invoke(
            app,
            [
                "probe",
                "--task",
                "task_123",
                "--instructions",
                "look at flakiness",
                "--background",
            ],
        )

    assert result.exit_code == 0, result.output
    assert captured["task_id"] == "task_123"
    assert captured["extra_instructions"] == "look at flakiness"
    assert "Probe queued" in result.output


def test_probe_requires_task(monkeypatch):
    _set_env(monkeypatch)
    result = CliRunner().invoke(app, ["probe", "--instructions", "x", "--background"])
    assert result.exit_code == 1
    assert "task" in result.output.lower()
