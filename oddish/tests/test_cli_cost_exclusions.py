from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from typer.testing import CliRunner  # noqa: E402

from oddish.cli.cost_exclusions import cost_exclusions_app  # noqa: E402

_MODEL_ROW = {
    "id": "m1",
    "model_name": "xai/grok-4",
    "label": "sponsored",
    "created_by": "admin_1",
    "created_at": "2026-08-15T00:00:00+00:00",
}
_EXPERIMENT_ROW = {
    "id": "x1",
    "experiment_id": "exp_1",
    "experiment_name": "glm sweep",
    "label": "comped",
    "created_by": "admin_1",
    "created_at": "2026-08-15T00:00:00+00:00",
}


def _invoke(args: list[str], responses: list[tuple[int, object]]):
    calls: list[dict] = []
    queue = list(responses)

    class _Resp:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

        @property
        def text(self):
            return json.dumps(self._payload)

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a, **k):
            return False

        def request(self, method, url, json=None, follow_redirects=False):
            calls.append({"method": method, "url": url, "json": json})
            status, payload = queue.pop(0)
            return _Resp(status, payload)

    runner = CliRunner()
    with (
        patch("oddish.cli.cost_exclusions.httpx.Client", _Client),
        patch("oddish.cli.cost_exclusions.require_api_key"),
        patch(
            "oddish.cli.cost_exclusions.get_api_url", return_value="http://localhost"
        ),
        patch("oddish.cli.cost_exclusions.get_auth_headers", return_value={}),
    ):
        result = runner.invoke(cost_exclusions_app, args)
    return result, calls


def test_list_fetches_both_axes_by_default():
    result, calls = _invoke(
        ["list", "--json"], [(200, [_MODEL_ROW]), (200, [_EXPERIMENT_ROW])]
    )
    assert result.exit_code == 0, result.output
    assert [c["url"] for c in calls] == [
        "http://localhost/admin/cost-excluded-models",
        "http://localhost/admin/cost-excluded-experiments",
    ]
    payload = json.loads(result.output)
    assert payload["models"][0]["model_name"] == "xai/grok-4"
    assert payload["experiments"][0]["experiment_id"] == "exp_1"


def test_list_can_scope_to_one_axis():
    result, calls = _invoke(["list", "--kind", "model", "--json"], [(200, [_MODEL_ROW])])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert "cost-excluded-models" in calls[0]["url"]


def test_list_rejects_unknown_kind():
    result, calls = _invoke(["list", "--kind", "task"], [])
    assert result.exit_code == 2
    assert calls == []


def test_add_model_posts_to_the_model_endpoint():
    result, calls = _invoke(
        ["add", "model", "xai/grok-4", "--label", "sponsored"], [(200, _MODEL_ROW)]
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/admin/cost-excluded-models")
    assert calls[0]["json"] == {"model_name": "xai/grok-4", "label": "sponsored"}


def test_add_experiment_uses_the_experiment_field():
    result, calls = _invoke(["add", "experiment", "glm sweep"], [(200, _EXPERIMENT_ROW)])
    assert result.exit_code == 0, result.output
    assert calls[0]["json"] == {"experiment": "glm sweep", "label": ""}


def test_add_surfaces_the_backend_detail_on_conflict():
    result, _ = _invoke(
        ["add", "experiment", "glm sweep"],
        [(409, {"detail": "experiment name is ambiguous; use the experiment id"})],
    )
    assert result.exit_code == 1
    assert "ambiguous" in result.output


def test_remove_resolves_a_row_id():
    result, calls = _invoke(
        ["remove", "model", "m1"], [(200, [_MODEL_ROW]), (200, {"deleted": "m1"})]
    )
    assert result.exit_code == 0, result.output
    assert calls[1]["method"] == "DELETE"
    assert calls[1]["url"].endswith("/admin/cost-excluded-models/m1")


def test_remove_resolves_the_model_name():
    result, calls = _invoke(
        ["remove", "model", "xai/grok-4"],
        [(200, [_MODEL_ROW]), (200, {"deleted": "m1"})],
    )
    assert result.exit_code == 0, result.output
    assert calls[1]["url"].endswith("/admin/cost-excluded-models/m1")


def test_remove_resolves_an_experiment_name():
    result, calls = _invoke(
        ["remove", "experiment", "glm sweep"],
        [(200, [_EXPERIMENT_ROW]), (200, {"deleted": "x1"})],
    )
    assert result.exit_code == 0, result.output
    assert calls[1]["url"].endswith("/admin/cost-excluded-experiments/x1")


def test_remove_refuses_an_ambiguous_name():
    twin = {**_EXPERIMENT_ROW, "id": "x2", "experiment_id": "exp_2"}
    result, calls = _invoke(
        ["remove", "experiment", "glm sweep"], [(200, [_EXPERIMENT_ROW, twin])]
    )
    assert result.exit_code == 1
    assert "matches 2 entries" in result.output
    assert len(calls) == 1


def test_remove_reports_no_match():
    result, calls = _invoke(["remove", "model", "nope"], [(200, [_MODEL_ROW])])
    assert result.exit_code == 1
    assert "No excluded model matches" in result.output
    assert len(calls) == 1


def test_operator_gate_failure_is_explained():
    result, _ = _invoke(["list", "--kind", "model"], [(403, {"detail": "forbidden"})])
    assert result.exit_code == 1
    assert "operator organization" in result.output


def test_pre_migration_503_is_explained():
    result, _ = _invoke(["list", "--kind", "model"], [(503, {"detail": "migrating"})])
    assert result.exit_code == 1
    assert "still migrating" in result.output
