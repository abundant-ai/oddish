"""Regression tests for metadata included with every ``oddish pull`` trial."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.cli.pull import _pull_trial  # noqa: E402


class _Response:
    def __init__(self, status_code: int, payload: object):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _Client:
    def __init__(self, routes):
        self.routes = routes

    def get(self, path, params=None):
        status, payload = self.routes.get(path, (404, {"detail": "not found"}))
        return _Response(status, payload)


def test_pull_trial_saves_full_detail_and_stored_summary(tmp_path):
    trial_id = "task-1"
    client = _Client(
        {
            f"/trials/{trial_id}": (
                200,
                {"id": trial_id, "analysis": {"classification": "GOOD_SUCCESS"}},
            ),
            f"/trials/{trial_id}/logs": (200, {"logs": "hello"}),
            f"/trials/{trial_id}/logs/structured": (200, {"agent": "hello"}),
            f"/trials/{trial_id}/result": (200, {"reward": 1}),
            f"/trials/{trial_id}/trajectory": (200, {"steps": []}),
            f"/trials/{trial_id}/trajectory/summary": (
                200,
                {"summary": {"schema_version": 6}, "refresh": None},
            ),
        }
    )

    result = _pull_trial(
        client,
        trial_id,
        tmp_path,
        include_logs=True,
        include_files=False,
        include_structured_logs=True,
    )

    root = tmp_path / "trials" / trial_id
    assert result["errors"] == 0
    assert json.loads((root / "trial.json").read_text())["id"] == trial_id
    assert (
        json.loads((root / "trajectory_summary.json").read_text())["summary"][
            "schema_version"
        ]
        == 6
    )


def test_pull_trial_allows_missing_optional_summary(tmp_path):
    trial_id = "task-2"
    client = _Client(
        {
            f"/trials/{trial_id}": (200, {"id": trial_id}),
            f"/trials/{trial_id}/result": (200, {"reward": 0}),
            f"/trials/{trial_id}/trajectory": (200, {"steps": []}),
        }
    )

    result = _pull_trial(
        client,
        trial_id,
        tmp_path,
        include_logs=False,
        include_files=False,
        include_structured_logs=False,
    )

    assert result["errors"] == 0
    assert not (tmp_path / "trials" / trial_id / "trajectory_summary.json").exists()


def test_pull_trial_uses_standalone_server_detail_fallback(tmp_path):
    trial_id = "task-3"
    client = _Client(
        {
            "/tasks/task/trials/3": (200, {"id": trial_id}),
            f"/trials/{trial_id}/result": (200, {"reward": 1}),
            f"/trials/{trial_id}/trajectory": (200, {"steps": []}),
        }
    )

    result = _pull_trial(
        client,
        trial_id,
        tmp_path,
        include_logs=False,
        include_files=False,
        include_structured_logs=False,
    )

    assert result["errors"] == 0
    assert (
        json.loads((tmp_path / "trials" / trial_id / "trial.json").read_text())["id"]
        == trial_id
    )
