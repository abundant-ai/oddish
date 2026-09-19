"""CUA / verifier LLM cost extraction and live settlement writes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oddish.core.harbor_artifacts import extract_trajectory_metrics
from oddish.costs.verifier_cost import (
    COMPONENT_JUDGE,
    COMPONENT_LOOP,
    COST_BACKFILL,
    COST_ESTIMATED,
    COST_NATIVE,
    ROUTE_ANTHROPIC,
    ROUTE_BEDROCK,
    VerifierCostDraft,
    build_verifier_cost_drafts,
    infer_verifier_route,
    load_cua_model_config,
    record_verifier_llm_costs,
    task_has_cua_signals,
    attempt_s3_prefix,
    _no_artifacts_sentinel,
    UNPRICED_NO_CUA_ARTIFACTS,
    UNPRICED_NO_CUA_CHECKED,
    backfill_batch_rank,
    should_graduate_backfill_miss,
    trial_needs_verifier_backfill,
    upsert_verifier_cost_rows,
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
    assert infer_verifier_route("eu.anthropic.claude-opus-4-7") == ROUTE_BEDROCK
    assert infer_verifier_route("apac.anthropic.claude-sonnet-4-6") == ROUTE_BEDROCK
    assert infer_verifier_route("anthropic.claude-opus-4-7") == ROUTE_BEDROCK


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


def test_loop_estimate_includes_step_cache_writes(tmp_path: Path) -> None:
    """Computer-1 often omits total_cost_usd; estimate must include step writes."""
    from oddish.model_pricing import estimate_cost_usd

    ux = tmp_path / "verifier" / "ux"
    ux.mkdir(parents=True)
    (ux / "trajectory.json").write_text(
        json.dumps(
            {
                "agent": {"name": "computer-1", "model_name": "anthropic/claude-opus-4-7"},
                "final_metrics": {
                    "total_prompt_tokens": 400,
                    "total_completion_tokens": 20,
                    "total_cached_tokens": 0,
                },
                "steps": [
                    {"metrics": {"extra": {"cache_creation_input_tokens": 100}}},
                    {"metrics": {"extra": {"cache_creation_input_tokens": 200}}},
                ],
            }
        ),
        encoding="utf-8",
    )
    (ux / "cua_judge_report.json").write_text(
        json.dumps(
            {
                "verifier_model": "anthropic/claude-opus-4-7",
                "verdicts": {"a": "PASS"},
            }
        ),
        encoding="utf-8",
    )
    drafts = build_verifier_cost_drafts(tmp_path)
    loop = next(d for d in drafts if d.component == COMPONENT_LOOP)
    assert loop.cache_write_tokens == 300
    assert loop.cost_source == COST_ESTIMATED
    assert loop.cost_usd == estimate_cost_usd(
        "anthropic/claude-opus-4-7", 400, 20, 0, 300
    )
    without_writes = estimate_cost_usd("anthropic/claude-opus-4-7", 400, 20, 0, 0)
    assert loop.cost_usd is not None
    assert without_writes is not None
    assert loop.cost_usd > without_writes


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


def test_build_drafts_missing_task_path_still_reads_artifacts(tmp_path: Path) -> None:
    """A deleted download is unknown, not 'not CUA' — still price job artifacts."""
    job = tmp_path / "job"
    ux = job / "verifier" / "ux"
    _write_atif(ux / "trajectory.json", cost=2.5, prompt=200, completion=80)
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
    drafts = build_verifier_cost_drafts(job, task_path=tmp_path / "deleted" / "task")
    assert len(drafts) == 2
    loop = next(d for d in drafts if d.component == COMPONENT_LOOP)
    assert loop.cost_usd == 2.5


def test_build_drafts_skips_existing_non_cua_task(tmp_path: Path) -> None:
    job = tmp_path / "job"
    ux = job / "verifier" / "ux"
    _write_atif(ux / "trajectory.json", cost=2.5)
    (ux / "cua_judge_report.json").write_text("{}", encoding="utf-8")
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.toml").write_text("[agent]\nname = 'nop'\n", encoding="utf-8")
    assert build_verifier_cost_drafts(job, task_path=task) == []


def test_attempt_s3_prefix_rewrites_sibling_attempts() -> None:
    key = "tasks/t1/trials/t1-1/attempt-3/"
    assert attempt_s3_prefix(key, 1) == "tasks/t1/trials/t1-1/attempt-1/"
    assert attempt_s3_prefix(key, 2) == "tasks/t1/trials/t1-1/attempt-2/"
    assert attempt_s3_prefix("tasks/t1/trials/t1-1/", 1) is None
    assert attempt_s3_prefix(None, 1) is None


def test_trial_result_cua_signal() -> None:
    from oddish.costs.verifier_cost import _trial_result_has_cua_signal

    assert _trial_result_has_cua_signal({"cua_rubric_score": 0.7}) is True
    assert _trial_result_has_cua_signal({"reward": 1.0}) is False
    assert _trial_result_has_cua_signal(None) is False


def test_backfill_skips_non_cua_even_when_uncovered() -> None:
    assert (
        trial_needs_verifier_backfill(
            has_cua_result_signal=False,
            covered_attempts=0,
            attempts=3,
        )
        is False
    )


def test_backfill_needs_uncovered_cua_attempts() -> None:
    assert (
        trial_needs_verifier_backfill(
            has_cua_result_signal=True,
            covered_attempts=0,
            attempts=1,
        )
        is True
    )
    assert (
        trial_needs_verifier_backfill(
            has_cua_result_signal=True,
            covered_attempts=1,
            attempts=2,
        )
        is True
    )
    assert (
        trial_needs_verifier_backfill(
            has_cua_result_signal=True,
            covered_attempts=1,
            attempts=1,
        )
        is False
    )


def test_backfill_sentinel_only_coverage_is_incomplete() -> None:
    """A no_cua_artifacts row is not real coverage, so the trial stays eligible."""
    assert (
        trial_needs_verifier_backfill(
            has_cua_result_signal=True,
            covered_attempts=0,
            attempts=1,
        )
        is True
    )
    assert backfill_batch_rank(has_any_verifier_row=False) == 0
    assert backfill_batch_rank(has_any_verifier_row=True) == 1
    assert backfill_batch_rank(has_any_verifier_row=False) < backfill_batch_rank(
        has_any_verifier_row=True
    )


def test_backfill_checked_sentinel_graduates() -> None:
    """A Harbor-subdirectory miss leaves the 200-trial pool."""
    assert should_graduate_backfill_miss(harbor_child_searched=True) is True
    assert should_graduate_backfill_miss(harbor_child_searched=False) is False
    checked = _no_artifacts_sentinel(
        checked=should_graduate_backfill_miss(harbor_child_searched=True)
    )
    assert checked.unpriced_reason == UNPRICED_NO_CUA_CHECKED
    assert checked.unpriced_reason != UNPRICED_NO_CUA_ARTIFACTS
    legacy = _no_artifacts_sentinel(
        checked=should_graduate_backfill_miss(harbor_child_searched=False)
    )
    assert legacy.unpriced_reason == UNPRICED_NO_CUA_ARTIFACTS
    assert (
        trial_needs_verifier_backfill(
            has_cua_result_signal=True,
            covered_attempts=1,
            attempts=1,
        )
        is False
    )


def test_no_artifacts_sentinel_is_replaceable() -> None:
    draft = _no_artifacts_sentinel()
    assert draft.cost_usd is None
    assert draft.unpriced_reason == UNPRICED_NO_CUA_ARTIFACTS
    assert draft.cost_source == COST_BACKFILL
    checked = _no_artifacts_sentinel(checked=True)
    assert checked.unpriced_reason == UNPRICED_NO_CUA_CHECKED
    assert checked.cost_source == COST_BACKFILL


def test_nop_shell_without_artifacts_writes_nothing(tmp_path: Path) -> None:
    (tmp_path / "agent").mkdir()
    assert build_verifier_cost_drafts(tmp_path) == []


@pytest.mark.asyncio
async def test_record_without_job_dir_is_noop() -> None:
    assert (
        await record_verifier_llm_costs(
            job_dir=None,
            task_path=None,
            trial_id="t-1",
            attempt=1,
            experiment_id=None,
            org_id=None,
            task_id=None,
            task_version_id=None,
        )
        == 0
    )


@pytest.mark.asyncio
async def test_record_without_artifacts_is_noop(tmp_path: Path) -> None:
    (tmp_path / "agent").mkdir()
    session = AsyncMock()
    assert (
        await record_verifier_llm_costs(
            job_dir=tmp_path,
            task_path=None,
            trial_id="t-1",
            attempt=1,
            experiment_id="e-1",
            org_id="o-1",
            task_id="task-1",
            task_version_id="tv-1",
            session=session,
        )
        == 0
    )
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_writes_one_row_per_draft(tmp_path: Path) -> None:
    ux = tmp_path / "verifier" / "ux"
    _write_atif(ux / "trajectory.json", cost=2.5, prompt=200, completion=80)
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
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=1))

    written = await record_verifier_llm_costs(
        job_dir=tmp_path,
        task_path=None,
        trial_id="t-1",
        attempt=2,
        experiment_id="e-1",
        org_id="o-1",
        task_id="task-1",
        task_version_id="tv-1",
        session=session,
    )
    assert written == 2
    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_upsert_restamps_created_at_when_replacing_sentinel() -> None:
    finished = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=1))
    draft = VerifierCostDraft(
        component=COMPONENT_LOOP,
        model="anthropic/claude-opus-4-7",
        route=ROUTE_ANTHROPIC,
        llm_key_hash=None,
        input_tokens=10,
        output_tokens=4,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=1.25,
        cost_source=COST_BACKFILL,
    )

    written = await upsert_verifier_cost_rows(
        session,
        drafts=[draft],
        trial_id="t-1",
        attempt=1,
        experiment_id="e-1",
        org_id="o-1",
        task_id="task-1",
        task_version_id="tv-1",
        created_at=finished,
    )

    assert written == 1
    stmt = session.execute.await_args.args[0]
    update = dict(stmt._post_values_clause.update_values_to_set)
    assert update["created_at"] == finished


@pytest.mark.asyncio
async def test_record_swallows_unexpected_errors(tmp_path: Path) -> None:
    with patch(
        "oddish.costs.verifier_cost.build_verifier_cost_drafts",
        side_effect=RuntimeError("boom"),
    ):
        assert (
            await record_verifier_llm_costs(
                job_dir=tmp_path,
                task_path=None,
                trial_id="t-1",
                attempt=1,
                experiment_id=None,
                org_id=None,
                task_id=None,
                task_version_id=None,
            )
            == 0
        )
