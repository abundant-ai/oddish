"""CUA / verifier LLM cost extraction (no database writes)."""

from __future__ import annotations

import json
from pathlib import Path

from oddish.costs.verifier_cost import (
    COMPONENT_JUDGE,
    COMPONENT_LOOP,
    COST_ESTIMATED,
    COST_NATIVE,
    ROUTE_ANTHROPIC,
    ROUTE_BEDROCK,
    build_verifier_cost_drafts,
    infer_verifier_route,
    load_cua_model_config,
    task_has_cua_signals,
)


def _write_atif(path: Path, *, cost: float, prompt: int = 100, completion: int = 50) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "final_metrics": {
                    "total_prompt_tokens": prompt,
                    "total_completion_tokens": completion,
                    "total_cached_tokens": 0,
                    "total_cost_usd": cost,
                },
                "steps": [{"step_id": 1}],
            }
        ),
        encoding="utf-8",
    )


def test_infer_route_prefers_anthropic_prefix() -> None:
    assert infer_verifier_route("anthropic/claude-opus-4-7") == ROUTE_ANTHROPIC
    assert infer_verifier_route("bedrock/anthropic.claude-opus") == ROUTE_BEDROCK
    assert infer_verifier_route("us.anthropic.claude-opus-4-7") == ROUTE_BEDROCK
    assert infer_verifier_route("global.anthropic.claude-opus-4-7") == ROUTE_BEDROCK


def test_task_has_cua_signals_inline_swe_m(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "cua_config.json").write_text(
        json.dumps({"model": "anthropic/claude-opus-4-7"}),
        encoding="utf-8",
    )
    assert task_has_cua_signals(tmp_path) is True
    assert task_has_cua_signals(tmp_path / "missing") is False


def test_load_cua_model_from_harbor_verifiers_block(tmp_path: Path) -> None:
    (tmp_path / "task.toml").write_text(
        """
[[verifiers]]
name = "correctness"
type = "shell"

[[verifiers]]
name = "ux"
type = "cua"
model = "anthropic/claude-opus-4-7"
judge_model = "anthropic/claude-sonnet-4-5"
timeout_sec = 600
""".strip(),
        encoding="utf-8",
    )
    cfg = load_cua_model_config(tmp_path)
    assert cfg["model"] == "anthropic/claude-opus-4-7"
    assert cfg["judge_model"] == "anthropic/claude-sonnet-4-5"


def test_build_drafts_loop_and_judge_from_ux_artifacts(tmp_path: Path) -> None:
    ux = tmp_path / "verifier" / "ux"
    _write_atif(ux / "trajectory.json", cost=2.5, prompt=200, completion=80)
    (ux / "cua_judge_report.json").write_text(
        json.dumps(
            {
                "verifier_model": "anthropic/claude-opus-4-7",
                "judge_model": "anthropic/claude-opus-4-7",
                "verdicts": [{"criterion": "a"}, {"criterion": "b"}],
            }
        ),
        encoding="utf-8",
    )

    drafts = build_verifier_cost_drafts(tmp_path)
    assert len(drafts) == 2
    loop = next(d for d in drafts if d.component == COMPONENT_LOOP)
    judge = next(d for d in drafts if d.component == COMPONENT_JUDGE)
    assert loop.cost_usd == 2.5
    assert loop.cost_source == COST_NATIVE
    assert loop.route == ROUTE_ANTHROPIC
    assert judge.cost_source == COST_ESTIMATED
    assert judge.input_tokens == 4000  # 2 criteria × 2000
    assert judge.cost_usd is not None or judge.unpriced_reason is not None


def test_judge_counts_dict_verdicts(tmp_path: Path) -> None:
    """SWE-M judge reports store verdicts as a criterion→PASS map."""
    ux = tmp_path / "verifier" / "ux"
    _write_atif(ux / "trajectory.json", cost=1.0)
    (ux / "cua_judge_report.json").write_text(
        json.dumps(
            {
                "verifier_model": "anthropic/claude-opus-4-7",
                "judge_model": "anthropic/claude-opus-4-7",
                "verdicts": {"auth_gate": "PASS", "projects": "PARTIAL"},
            }
        ),
        encoding="utf-8",
    )
    drafts = build_verifier_cost_drafts(tmp_path)
    judge = next(d for d in drafts if d.component == COMPONENT_JUDGE)
    assert judge.input_tokens == 4000


def test_loop_model_falls_back_to_trajectory_agent(tmp_path: Path) -> None:
    """Model comes from ATIF when the judge report omits it."""
    ux = tmp_path / "verifier" / "ux"
    ux.mkdir(parents=True)
    (ux / "trajectory.json").write_text(
        json.dumps(
            {
                "agent": {"name": "computer-1", "model_name": "anthropic/claude-fable-5-1"},
                "final_metrics": {
                    "total_prompt_tokens": 100,
                    "total_completion_tokens": 20,
                    "total_cached_tokens": 0,
                    "total_cost_usd": 0.5,
                },
                "steps": [],
            }
        ),
        encoding="utf-8",
    )
    (ux / "cua_judge_report.json").write_text(
        json.dumps({"verdicts": {"a": "PASS"}}),
        encoding="utf-8",
    )
    drafts = build_verifier_cost_drafts(tmp_path)
    loop = next(d for d in drafts if d.component == COMPONENT_LOOP)
    assert loop.model == "anthropic/claude-fable-5-1"
    assert loop.route == ROUTE_ANTHROPIC
    assert loop.cost_usd == 0.5


def test_build_drafts_under_harbor_trial_subdir(tmp_path: Path) -> None:
    """Harbor writes artifacts under a trial-name subdirectory."""
    ux = tmp_path / "task__abc123" / "verifier" / "ux"
    _write_atif(ux / "trajectory.json", cost=1.5, prompt=100, completion=40)
    (ux / "cua_judge_report.json").write_text(
        json.dumps(
            {
                "verifier_model": "anthropic/claude-opus-4-7",
                "judge_model": "anthropic/claude-opus-4-7",
                "verdicts": [{"criterion": "a"}],
            }
        ),
        encoding="utf-8",
    )
    drafts = build_verifier_cost_drafts(tmp_path)
    assert len(drafts) == 2
    loop = next(d for d in drafts if d.component == COMPONENT_LOOP)
    assert loop.cost_usd == 1.5


def test_nop_shell_without_artifacts_writes_nothing(tmp_path: Path) -> None:
    (tmp_path / "agent").mkdir()
    assert build_verifier_cost_drafts(tmp_path) == []
