"""CUA / verifier LLM cost extraction and ledger invariants."""

from __future__ import annotations

import json
from pathlib import Path

from oddish.core.harbor_artifacts import extract_trajectory_metrics
from oddish.costs.verifier_cost import (
    COMPONENT_JUDGE,
    COMPONENT_LOOP,
    COST_ESTIMATED,
    COST_NATIVE,
    ROUTE_ANTHROPIC,
    ROUTE_BEDROCK,
    build_verifier_cost_drafts,
    infer_verifier_route,
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


def test_extract_trajectory_metrics_skips_verifier_tree(tmp_path: Path) -> None:
    """Solver cost must not absorb CUA Computer1 ATIF usage."""
    _write_atif(tmp_path / "verifier" / "ux" / "trajectory.json", cost=9.99, prompt=999)
    agent = tmp_path / "agent"
    _write_atif(agent / "trajectory.json", cost=1.25, prompt=40, completion=10)

    metrics = extract_trajectory_metrics(tmp_path)
    assert metrics.cost_usd == 1.25
    assert metrics.input_tokens == 40


def test_extract_trajectory_metrics_ignores_cua_only_tree(tmp_path: Path) -> None:
    _write_atif(tmp_path / "verifier" / "trajectory.json", cost=5.0)
    metrics = extract_trajectory_metrics(tmp_path)
    assert metrics.has_trajectory is False
    assert metrics.cost_usd is None


def test_task_has_cua_signals_inline_swe_m(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "cua_config.json").write_text(
        json.dumps({"model": "anthropic/claude-opus-4-7"}),
        encoding="utf-8",
    )
    assert task_has_cua_signals(tmp_path) is True
    assert task_has_cua_signals(tmp_path / "missing") is False


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


def test_unique_index_is_partial_on_live_rows() -> None:
    """Failed attempts keep spend; SUCCESS must not replace them via upsert."""
    from oddish.db.models import VerifierCostModel

    uniques = [
        idx
        for idx in VerifierCostModel.__table__.indexes
        if idx.unique
    ]
    assert uniques
    cols = {c.name for c in uniques[0].columns}
    assert cols == {"trial_id", "attempt", "component"}


def test_nop_shell_without_artifacts_writes_nothing(tmp_path: Path) -> None:
    (tmp_path / "agent").mkdir()
    assert build_verifier_cost_drafts(tmp_path) == []
