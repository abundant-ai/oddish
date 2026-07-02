"""Tests for the full browse-filter flags on ``oddish ls``.

Mirrors ``test_cli_ls_tags.py``: fakes the HTTP client, invokes the command, and
asserts the CLI serializes flags into the exact query params the
``GET /tasks/browse`` endpoint expects.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import typer  # noqa: E402
from typer.testing import CliRunner  # noqa: E402


def _invoke(args: list[str]) -> dict:
    """Run ``ls`` with ``args`` against a fake client; return the captured params."""
    import importlib

    ls_module = importlib.import_module("oddish.cli.ls")
    captured: dict = {}

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"items": [], "has_more": False, "limit": 25, "offset": 0}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a, **k):
            return False

        def get(self, url, params):
            captured["params"] = dict(params)
            return _FakeResponse()

    app = typer.Typer()
    app.command()(ls_module.ls)

    runner = CliRunner()
    with patch("oddish.cli.ls.httpx.Client", _FakeClient):
        with patch("oddish.cli.ls.require_api_key"):
            with patch("oddish.cli.ls.get_api_url", return_value="http://localhost"):
                with patch("oddish.cli.ls.get_auth_headers", return_value={}):
                    result = runner.invoke(app, [*args, "--json"])
    captured["exit_code"] = result.exit_code
    captured["output"] = result.output
    return captured


def test_ls_csv_multiselects_join_repeated_values():
    captured = _invoke(
        [
            "--status",
            "COMPLETED",
            "--status",
            "RUNNING",
            "--agent",
            "cursor-cli",
            "--model",
            "gpt-5",
            "--agent-model",
            "cursor-cli:gpt-5",
            "--provider",
            "openai",
            "--environment",
            "swe",
            "--trial-status",
            "SUCCESS",
            "--origin",
            "oddish",
            "--analysis-classification",
            "flaky",
            "--experiment-id",
            "exp_1",
            "--harbor-sha",
            "abc123",
            "--harbor-stage",
            "build",
            "--priority",
            "HIGH",
            "--verdict-status",
            "SUCCESS",
        ]
    )
    assert captured["exit_code"] == 0
    p = captured["params"]
    assert p["statuses"] == "COMPLETED,RUNNING"
    assert p["agents"] == "cursor-cli"
    assert p["models"] == "gpt-5"
    assert p["agent_models"] == "cursor-cli:gpt-5"
    assert p["providers"] == "openai"
    assert p["environments"] == "swe"
    assert p["trial_statuses"] == "SUCCESS"
    assert p["origins"] == "oddish"
    assert p["analysis_classifications"] == "flaky"
    assert p["experiment_ids"] == "exp_1"
    assert p["harbor_shas"] == "abc123"
    assert p["harbor_stages"] == "build"
    assert p["priorities"] == "HIGH"
    assert p["verdict_statuses"] == "SUCCESS"


def test_ls_tristate_booleans_serialize_true_false():
    captured = _invoke(
        [
            "--has-link",
            "--no-has-error",
            "--has-trajectory",
            "--trial-is-probe",
            "--no-run-analysis",
            "--run-probe",
        ]
    )
    assert captured["exit_code"] == 0
    p = captured["params"]
    assert p["has_link"] == "true"
    assert p["has_error"] == "false"
    assert p["has_trajectory"] == "true"
    assert p["trial_is_probe"] == "true"
    assert p["run_analysis"] == "false"
    assert p["run_probe"] == "true"


def test_ls_omits_unset_booleans():
    captured = _invoke(["--status", "COMPLETED"])
    assert captured["exit_code"] == 0
    p = captured["params"]
    for key in (
        "has_link",
        "has_error",
        "has_trajectory",
        "trial_is_probe",
        "run_analysis",
        "run_probe",
    ):
        assert key not in p


def test_ls_numeric_ranges_and_sort():
    captured = _invoke(
        [
            "--min-tokens",
            "100",
            "--max-tokens",
            "5000",
            "--reward-min",
            "0.5",
            "--avg-score-min",
            "80",
            "--runtime-total-max",
            "120.5",
            "--total-trials-min",
            "3",
            "--pass-rate-min",
            "50",
            "--sort",
            "avg_score_desc",
        ]
    )
    assert captured["exit_code"] == 0
    p = captured["params"]
    assert p["min_tokens"] == 100
    assert p["max_tokens"] == 5000
    assert p["reward_min"] == 0.5
    assert p["avg_score_min"] == 80
    assert p["runtime_total_max"] == 120.5
    assert p["total_trials_min"] == 3
    assert p["pass_rate_min"] == 50
    assert p["sort"] == "avg_score_desc"


def test_ls_compare_and_top_and_or_groups():
    or_groups = '[{"agents": ["cursor-cli"], "reward_min": 0.5}]'
    captured = _invoke(
        [
            "--compare-by",
            "agent",
            "--compare-a",
            "cursor-cli",
            "--compare-b",
            "gemini-cli",
            "--compare-metric",
            "reward",
            "--compare-agg",
            "best",
            "--compare-margin",
            "5",
            "--compare-margin-unit",
            "pct",
            "--top-by",
            "model",
            "--top-value",
            "gpt-5",
            "--top-metric",
            "reward",
            "--or-groups",
            or_groups,
        ]
    )
    assert captured["exit_code"] == 0
    p = captured["params"]
    assert p["compare_by"] == "agent"
    assert p["compare_a"] == "cursor-cli"
    assert p["compare_b"] == "gemini-cli"
    assert p["compare_metric"] == "reward"
    assert p["compare_agg"] == "best"
    assert p["compare_margin"] == 5
    assert p["compare_margin_unit"] == "pct"
    assert p["top_by"] == "model"
    assert p["top_value"] == "gpt-5"
    assert p["top_metric"] == "reward"
    assert p["or_groups"] == or_groups


def test_ls_compare_defaults_when_only_pair_given():
    # A distinct A/B pair with no --compare-by/-metric/-agg must still forward the
    # defaulted trio (else the backend silently skips the comparison).
    captured = _invoke(["--compare-a", "cursor-cli", "--compare-b", "gemini-cli"])
    assert captured["exit_code"] == 0
    p = captured["params"]
    assert p["compare_by"] == "agent"
    assert p["compare_metric"] == "reward"
    assert p["compare_agg"] == "best"
    assert p["compare_a"] == "cursor-cli"
    assert p["compare_b"] == "gemini-cli"
    # No margin flag → no margin params.
    assert "compare_margin" not in p
    assert "compare_margin_unit" not in p


def test_ls_compare_omitted_when_pair_incomplete_or_equal():
    # Lone A (no B) emits nothing — mirrors the UI dropping an incomplete pair.
    lone = _invoke(["--compare-a", "cursor-cli"])
    assert lone["exit_code"] == 0
    assert not any(k.startswith("compare_") for k in lone["params"])

    # Identical A == B is not a comparison; emit nothing.
    same = _invoke(["--compare-a", "cursor-cli", "--compare-b", "cursor-cli"])
    assert same["exit_code"] == 0
    assert not any(k.startswith("compare_") for k in same["params"])


def test_ls_top_defaults_when_only_value_given():
    captured = _invoke(["--top-value", "gpt-5"])
    assert captured["exit_code"] == 0
    p = captured["params"]
    assert p["top_by"] == "agent"
    assert p["top_metric"] == "reward"
    assert p["top_value"] == "gpt-5"


def test_ls_top_omitted_without_value():
    # --top-by/-metric without --top-value must not emit a partial top block.
    captured = _invoke(["--top-by", "model", "--top-metric", "reward"])
    assert captured["exit_code"] == 0
    assert not any(k.startswith("top_") for k in captured["params"])


def test_ls_created_within_resolves_to_created_after():
    captured = _invoke(["--created-within", "24h"])
    assert captured["exit_code"] == 0
    p = captured["params"]
    assert "created_after" in p
    assert "created_within" not in p


def test_ls_created_within_rejects_unknown_token():
    captured = _invoke(["--created-within", "bogus"])
    assert captured["exit_code"] != 0
