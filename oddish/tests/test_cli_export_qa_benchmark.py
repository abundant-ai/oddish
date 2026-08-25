"""Tests for ``oddish export-qa-benchmark``."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import typer  # noqa: E402
from typer.testing import CliRunner  # noqa: E402


class _Response:
    def __init__(self, status_code: int, payload: object):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    @property
    def text(self):
        return json.dumps(self._payload)


class _Client:
    def __init__(self, response):
        self.response = response
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get(self, path, params=None):
        assert path == "/admin/qa-feedback-export"
        self.params = params
        return self.response


def _item(trial_id: str, grader_id: str) -> dict:
    return {
        "trial_id": trial_id,
        "grader_trial_id": grader_id,
        "task_id": trial_id.rsplit("-", 1)[0],
        "task_version_id": "v1",
        "experiment_id": "exp",
        "classification": "GOOD_SUCCESS",
        "human_vote": "agree",
        "review_note": "",
        "reviewed_at": "2026-08-25T20:00:00Z",
        "vote_count": 1,
        "reward": 1.0,
        "solver_agent": "codex",
        "solver_model": "openai/gpt-5",
        "judge_agent": "claude-code",
        "judge_model": "anthropic/claude-sonnet-4-6",
    }


def _invoke(payload, args, *, status_code=200, pull_result=None):
    import importlib

    module = importlib.import_module("oddish.cli.export_qa_benchmark")
    client = _Client(_Response(status_code, payload))
    pulled: list[str] = []

    def fake_pull(_client, trial_id, output_root):
        pulled.append(trial_id)
        trial_root = output_root / "trials" / trial_id
        trial_root.mkdir(parents=True, exist_ok=True)
        (trial_root / "trial.json").write_text("{}", encoding="utf-8")
        if pull_result is not None:
            return {"trial_id": trial_id, **pull_result}
        return {"trial_id": trial_id, "errors": 0}

    app = typer.Typer()
    app.command(name="export-qa-benchmark")(module.export_qa_benchmark)
    with patch.object(module, "_make_client", return_value=client):
        with patch.object(module, "_download_one", side_effect=fake_pull):
            with patch.object(module, "require_api_key"):
                with patch.object(module, "get_api_url", return_value="http://prod"):
                    result = CliRunner().invoke(app, args)
    return result, pulled, client


def test_export_downloads_each_solver_and_shared_grader_once(tmp_path):
    items = [_item("task-a-1", "task-a-9"), _item("task-a-2", "task-a-9")]
    payload = {
        "requested_limit": 2,
        "eligible_total": 7,
        "returned_count": 2,
        "items": items,
    }
    out = tmp_path / "bundle"

    result, pulled, client = _invoke(
        payload,
        ["--limit", "2", "--out", str(out), "--no-archive", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert client.params == {"limit": 2}
    assert sorted(pulled) == ["task-a-1", "task-a-2", "task-a-9"]
    samples = [
        json.loads(line) for line in (out / "samples.jsonl").read_text().splitlines()
    ]
    assert len(samples) == 2
    assert samples[0]["human_vote"] == "agree"
    assert samples[0]["grader_trial_path"] == "trials/task-a-9"
    assert "agree` means the QA classification" in (out / "README.md").read_text()
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["exported_labels"] == 2
    assert manifest["unique_trials_downloaded"] == 3
    assert manifest["archive"] is None


def test_export_refuses_silent_short_dataset(tmp_path):
    payload = {
        "requested_limit": 300,
        "eligible_total": 12,
        "returned_count": 12,
        "items": [_item(f"task-{i}", "grader-1") for i in range(12)],
    }
    out = tmp_path / "bundle"

    result, pulled, _ = _invoke(payload, ["--limit", "300", "--out", str(out)])

    assert result.exit_code == 1
    assert "Production has 12 eligible" in result.output
    assert pulled == []
    assert not out.exists()


def test_export_allow_fewer_uses_available_records(tmp_path):
    payload = {
        "requested_limit": 3,
        "eligible_total": 1,
        "returned_count": 1,
        "items": [_item("task-1", "task-9")],
    }
    out = tmp_path / "bundle"

    result, pulled, _ = _invoke(
        payload,
        [
            "--limit",
            "3",
            "--allow-fewer",
            "--out",
            str(out),
            "--no-archive",
        ],
    )

    assert result.exit_code == 0, result.output
    assert sorted(pulled) == ["task-1", "task-9"]


def test_export_surfaces_operator_auth_failure(tmp_path):
    result, pulled, _ = _invoke(
        {"detail": "Operator access required"},
        ["--out", str(tmp_path / "bundle")],
        status_code=403,
    )

    assert result.exit_code == 1
    assert "full-scope API key" in result.output
    assert pulled == []


def test_export_keeps_partial_directory_and_refuses_archive_on_download_error(tmp_path):
    payload = {
        "requested_limit": 1,
        "eligible_total": 1,
        "returned_count": 1,
        "items": [_item("task-1", "task-9")],
    }
    out = tmp_path / "bundle"

    result, _, _ = _invoke(
        payload,
        ["--limit", "1", "--out", str(out)],
        pull_result={"errors": 1},
    )

    assert result.exit_code == 1
    assert (out / "manifest.json").exists()
    assert not out.with_suffix(".tar.gz").exists()
    manifest = json.loads((out / "manifest.json").read_text())
    assert len(manifest["failed_trials"]) == 2
