"""CLI submits -m as typed and keeps --json as one stdout document."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.cli import app  # noqa: E402
from oddish.cli.api import load_sweep_config  # noqa: E402
from oddish.core.sweeps import validate_sweep_submission  # noqa: E402
from oddish.schemas import AgentModelPair, TaskSweepSubmission  # noqa: E402

run_mod = importlib.import_module("oddish.cli.run")

_SWEEP_RESULT = {
    "id": "task-1",
    "trials_count": 1,
    "experiment_id": "exp-1",
    "experiment_name": "abt-1153",
    "providers": {"fireworks": 1},
}


def _capture(monkeypatch):
    payloads: list[dict] = []

    def _post(_api_url, payload):
        payloads.append(payload)
        return dict(_SWEEP_RESULT)

    monkeypatch.setattr(run_mod, "post_sweep_payload", _post)
    monkeypatch.setattr(run_mod, "get_task_summary", lambda *a, **k: None)
    return payloads


def _run(**kwargs):
    defaults = dict(
        existing_task_id="task-1",
        agent="mini-swe-agent",
        model="deepseek-v4-flash",
        n_trials=1,
        json_output=False,
    )
    defaults.update(kwargs)
    return run_mod.run(**defaults)


def test_model_is_submitted_verbatim_regardless_of_local_keys(monkeypatch):
    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    for combo in (
        {},
        {"FIREWORKS_API_KEY": "sk-fw"},
        {"DEEPSEEK_API_KEY": "sk-ds"},
        {"FIREWORKS_API_KEY": "sk-fw", "DEEPSEEK_API_KEY": "sk-ds"},
    ):
        monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        for name, value in combo.items():
            monkeypatch.setenv(name, value)
        payloads = _capture(monkeypatch)
        _run()
        assert payloads[0]["configs"][0]["model"] == "deepseek-v4-flash"
        assert "provider" not in payloads[0]["configs"][0]


def test_provider_and_allow_unknown_flags_and_json_stdout(monkeypatch):
    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    payloads = _capture(monkeypatch)
    _run(provider="deepseek", allow_unknown_model=True)
    config = payloads[0]["configs"][0]
    assert config["model"] == "deepseek-v4-flash"
    assert config["provider"] == "deepseek"
    assert config["allow_unknown_model"] is True

    monkeypatch.setattr(
        run_mod, "post_sweep_payload", lambda *a, **k: dict(_SWEEP_RESULT)
    )
    result = CliRunner().invoke(
        app,
        [
            "run",
            "--task",
            "task-1",
            "-a",
            "mini-swe-agent",
            "-m",
            "deepseek-v4-flash",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    json.loads(result.stdout)


def test_quoted_yaml_false_does_not_open_allowlist(tmp_path):
    path = tmp_path / "sweep.yaml"
    path.write_text(
        "agents:\n"
        "  - name: mini-swe-agent\n"
        "    model_name: fireworks/nope\n"
        "    n_trials: 1\n"
        "    allow_unknown_model: \"false\"\n"
    )
    pair = AgentModelPair(**load_sweep_config(path)["agents"][0])
    assert pair.allow_unknown_model is False
    with pytest.raises(Exception) as exc:
        validate_sweep_submission(
            TaskSweepSubmission(task_id="task-1", configs=[pair])
        )
    assert "Unknown fireworks model" in str(getattr(exc.value, "detail", exc.value))
