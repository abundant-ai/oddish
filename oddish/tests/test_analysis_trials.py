"""Tests for analysis trials.

Each test checks one rule. The rule is in the test name and the first line.
"""

import os
import uuid

import pytest

from oddish.analyze.models import TaskVerdictModel
from oddish.db.models import TrialModel
from oddish.filters.trial_predicates import EligibleTrialScope
from oddish.core.trial_facets import facet_rows_for_trial
from oddish.workers.analysis_trials import (
    _classification_from_analysis,
    build_audit_brief,
    build_qa_brief,
    has_verdict_evidence,
    is_analysis_kind,
)

URL = os.environ.get("ODDISH_DATABASE_URL")

GOOD_ANALYSIS = {
    "trial_name": "t-1",
    "classification": "BAD_FAILURE",
    "subtype": "Verifier bug",
    "evidence": "e",
    "root_cause": "r",
    "recommendation": "x",
    "reward": 0.0,
    "action_items": [],
    "exploitation": [],
}


def test_the_analysis_kinds_are_known():
    """qa and audit are analysis kinds. agent is not."""
    for kind in ("qa", "audit"):
        assert is_analysis_kind(kind)
    assert not is_analysis_kind("agent")
    assert not is_analysis_kind(None)


def test_the_qa_brief_tells_the_agent_everything_it_needs():
    """The brief must name each trial, the output file, the labels, and the
    verdict fields. If one is missing, the QA agent cannot do its job."""
    brief = build_qa_brief(
        task_name="demo",
        trial_ids=["t-1", "t-2"],
        pre_trial_items=[{"id": "a1", "description": "leaky test"}],
    )
    assert "- t-1" in brief
    assert "- t-2" in brief
    assert "qa_result.json" in brief
    assert "leaky test" in brief
    assert "GOOD_SUCCESS|BAD_SUCCESS" in brief
    for field in TaskVerdictModel.model_json_schema()["properties"]:
        assert field in brief


def test_the_audit_brief_names_its_output_file():
    """The audit agent must know where to write, and must not solve the task."""
    brief = build_audit_brief(task_name="demo")
    assert "audit_result.json" in brief
    assert "Do not solve the task" in brief


def _qa_check_payload(trial_ids: list[str], *, with_verdict: bool = False) -> dict:
    from oddish.workers.analysis_trials import analysis_check_payload

    return analysis_check_payload(
        "qa",
        {"analysis_payload": {"trial_ids": trial_ids, "with_verdict": with_verdict}},
    )


def _good_qa_entry(trial_id: str) -> dict:
    return {
        "trial_id": trial_id,
        "analysis": dict(GOOD_ANALYSIS, trial_name=trial_id),
        "trajectory_summary": {
            "summary": "The agent edited the file and the verifier agreed.",
            "highlights": [{"step_id": 1, "title": "edit", "why": "it landed"}],
            "components": [
                {
                    "step_ids": [1],
                    "trajectory_component": "implementing",
                    "action": "edit",
                    "purpose": "build",
                    "summary": "One edit.",
                }
            ],
        },
    }


def test_the_overlay_replaces_the_whole_task(tmp_path):
    """An analysis trial is a regular trial on our own task. Nothing of the
    audited task survives into the sandbox: not its image, not its verifier,
    not its hidden files. Our verifier stages /logs/<artifact> and validates
    it against the contract pinned at trial creation, so a missing or
    malformed artifact fails the trial and retries re-run the agent."""
    import json

    from oddish.worker.probe_staging import apply_analysis_overlay

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "expensive_llm_judge.py").write_text("x")
    (tmp_path / "solution").mkdir()
    (tmp_path / "solution" / "fix.patch").write_text("x")
    (tmp_path / "environment").mkdir()
    (tmp_path / "environment" / "Dockerfile").write_text("FROM giant-java-image")
    (tmp_path / "instruction.md").write_text("original")
    payload = _qa_check_payload(["t-1"])
    apply_analysis_overlay(
        tmp_path, brief="the brief", artifact="qa_result.json", check_payload=payload
    )

    assert (tmp_path / "instruction.md").read_text() == "the brief"
    assert not (tmp_path / "tests" / "expensive_llm_judge.py").exists()
    assert not (tmp_path / "solution").exists()
    dockerfile = (tmp_path / "environment" / "Dockerfile").read_text()
    assert "python:3.13-slim" in dockerfile
    assert "nodejs" in dockerfile
    assert "oddish-analysis" in (tmp_path / "task.toml").read_text()
    test_sh = (tmp_path / "tests" / "test.sh").read_text()
    assert "/logs/qa_result.json" in test_sh
    assert "exit 1" in test_sh
    assert 'cp "$SRC" "$OUT/qa_result.json"' in test_sh
    # The verifier enforces the pinned contract with the same validator the
    # importer runs: both files must be staged beside test.sh.
    assert "analysis_result_check.py" in test_sh
    staged_expected = json.loads((tmp_path / "tests" / "expected.json").read_text())
    assert staged_expected == payload
    validator = (tmp_path / "tests" / "analysis_result_check.py").read_text()
    assert "def check_analysis_result" in validator


def test_a_correct_analysis_is_accepted():
    """A well-formed analysis from the QA agent parses into a classification."""
    parsed = _classification_from_analysis(GOOD_ANALYSIS)
    assert parsed is not None
    assert parsed.classification.value == "BAD_FAILURE"


@pytest.mark.parametrize(
    "broken",
    [
        {},
        {"classification": "NOT_A_LABEL"},
        {"classification": "BAD_FAILURE", "action_items": [{"bogus": True}]},
    ],
)
def test_a_broken_analysis_is_rejected_not_stored(broken):
    """A malformed analysis must parse to None. It must never reach the DB."""
    assert _classification_from_analysis(broken) is None


def test_trial_filters_hide_analysis_trials_by_default():
    """Every surface that uses the shared filter sees agent trials only,
    unless it opts in."""
    default = EligibleTrialScope(membership=[]).clauses()
    assert any("kind" in str(c) for c in default)
    opted_in = EligibleTrialScope(membership=[], include_non_agent_kinds=True)
    assert not any("kind" in str(c) for c in opted_in.clauses())


def test_browse_filters_never_learn_analysis_trial_values():
    """A QA trial must not add its agent or model to the browse dropdowns."""
    kwargs = dict(org_id="org", agent="claude-code", model="m")
    assert facet_rows_for_trial(**kwargs)
    assert facet_rows_for_trial(**kwargs, trial_kind="qa") == set()
    assert facet_rows_for_trial(**kwargs, trial_kind="audit") == set()


@pytest.mark.asyncio
async def test_a_task_gets_exactly_one_qa_trial():
    """Needs a database. Checks three rules in order:
    1. QA does not start while an agent trial still runs.
    2. When the last agent trial ends, exactly one QA trial appears,
       even if two workers race.
    3. The QA trial itself never triggers more QA."""
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from oddish.db import (
        TaskStatus,
        TrialStatus,
        VerdictStatus,
        get_session,
        init_db,
    )
    from oddish.db.models import ExperimentModel, TaskModel
    from oddish.queue import maybe_start_qa_stage
    from sqlalchemy import select, text

    await init_db()
    run = uuid.uuid4().hex[:8]
    task_id = f"qa-barrier-{run}"
    async with get_session() as session:
        experiment = ExperimentModel(name=f"exp-{run}")
        session.add(experiment)
        session.add(
            TaskModel(
                id=task_id,
                name=task_id,
                user="u",
                task_path="p",
                status=TaskStatus.RUNNING,
                run_analysis=True,
            )
        )
        await session.flush()
        await session.execute(
            text(
                "INSERT INTO task_experiments (task_id, experiment_id, created_at) "
                "VALUES (:t, :e, NOW())"
            ),
            {"t": task_id, "e": experiment.id},
        )
        for i, status in enumerate((TrialStatus.SUCCESS, TrialStatus.RUNNING), start=1):
            session.add(
                TrialModel(
                    id=f"{task_id}-{i}",
                    name=f"{task_id}-{i}",
                    task_id=task_id,
                    experiment_id=experiment.id,
                    agent="claude-code",
                    provider="local",
                    queue_key="q",
                    status=status,
                    attempts=1,
                    max_attempts=3,
                )
            )
        await session.commit()

    # Rule 1: one trial still runs, so QA does not start.
    async with get_session() as session:
        assert await maybe_start_qa_stage(session, f"{task_id}-1") is False
        await session.commit()

    async with get_session() as session:
        trial = await session.get(TrialModel, f"{task_id}-2")
        trial.status = TrialStatus.SUCCESS
        await session.commit()

    # Rule 2: all trials are done. The first caller starts QA. The second
    # caller sees QA already started and does nothing.
    async with get_session() as session:
        assert await maybe_start_qa_stage(session, f"{task_id}-1") is True
        await session.commit()
    async with get_session() as session:
        assert await maybe_start_qa_stage(session, f"{task_id}-1") is False
        await session.commit()

    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        assert task.status == TaskStatus.VERDICT_PENDING
        assert task.verdict_status == VerdictStatus.QUEUED
        qa_trials = (
            (
                await session.execute(
                    select(TrialModel).where(
                        TrialModel.task_id == task_id, TrialModel.kind == "qa"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(qa_trials) == 1
        brief = qa_trials[0].harbor_config["extra_instructions"]
        assert f"{task_id}-1" in brief
        assert f"{task_id}-2" in brief

    # Rule 3: the QA trial is not an agent trial, so it triggers nothing.
    async with get_session() as session:
        assert await maybe_start_qa_stage(session, qa_trials[0].id) is False


@pytest.mark.asyncio
async def test_qa_admission_waits_for_the_audit():
    """Needs a database. The QA brief snapshots the audit findings at
    creation and is never rebuilt, so automatic admission must defer while
    an audit trial is live -- and the audit's own settlement must then
    start QA, or a task whose last agent trial settled mid-audit would
    never get one."""
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from sqlalchemy import select, text

    from oddish.db import TaskStatus, TrialStatus, get_session, init_db
    from oddish.db.models import ExperimentModel, TaskModel
    from oddish.queue import maybe_start_qa_stage
    from oddish.workers.analysis_trials import handle_analysis_trial_settled

    await init_db()
    run = uuid.uuid4().hex[:8]
    task_id = f"qa-audit-gate-{run}"
    agent_id = f"{task_id}-1"
    audit_id = f"{task_id}-2"
    async with get_session() as session:
        experiment = ExperimentModel(name=f"exp-{run}")
        session.add(experiment)
        session.add(
            TaskModel(
                id=task_id,
                name=task_id,
                user="u",
                task_path="p",
                status=TaskStatus.RUNNING,
                run_analysis=True,
            )
        )
        await session.flush()
        await session.execute(
            text(
                "INSERT INTO task_experiments (task_id, experiment_id, created_at) "
                "VALUES (:t, :e, NOW())"
            ),
            {"t": task_id, "e": experiment.id},
        )
        session.add(
            TrialModel(
                id=agent_id,
                name=agent_id,
                task_id=task_id,
                experiment_id=experiment.id,
                agent="claude-code",
                provider="local",
                queue_key="q",
                status=TrialStatus.SUCCESS,
                attempts=1,
                max_attempts=3,
            )
        )
        session.add(
            TrialModel(
                id=audit_id,
                name=audit_id,
                task_id=task_id,
                experiment_id=experiment.id,
                agent="claude-code",
                provider="local",
                queue_key="q",
                kind="audit",
                status=TrialStatus.RUNNING,
                attempts=1,
                max_attempts=3,
            )
        )
        await session.commit()

    # All agent trials are done, but the audit is live: admission defers.
    async with get_session() as session:
        assert await maybe_start_qa_stage(session, agent_id) is False
    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        assert task.status == TaskStatus.RUNNING
        qa_count = len(
            (
                await session.execute(
                    select(TrialModel).where(
                        TrialModel.task_id == task_id, TrialModel.kind == "qa"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert qa_count == 0

    # The audit settles; its settlement re-enters admission and starts QA.
    async with get_session() as session:
        audit = await session.get(TrialModel, audit_id)
        audit.status = TrialStatus.FAILED
        await session.commit()
    await handle_analysis_trial_settled(audit_id)

    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        assert task.status == TaskStatus.VERDICT_PENDING
        qa_trials = (
            (
                await session.execute(
                    select(TrialModel).where(
                        TrialModel.task_id == task_id, TrialModel.kind == "qa"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(qa_trials) == 1


@pytest.mark.asyncio
async def test_generic_retry_refuses_analysis_trials():
    """Needs a database. "Rerun trials" hits the generic retry endpoint;
    a qa/audit row must be refused there. Retrying one would copy its kind
    and stale brief into a new trial, knock the task back to RUNNING, and
    discard a published verdict -- the task-level QA endpoints are the
    rerun path for analysis."""
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from fastapi import HTTPException

    from oddish.core.endpoints.trials import retry_trial_core
    from oddish.db import TaskStatus, TrialStatus, get_session, init_db
    from oddish.db.models import ExperimentModel, TaskModel

    await init_db()
    run = uuid.uuid4().hex[:8]
    task_id = f"qa-retry-guard-{run}"
    qa_id = f"{task_id}-1"
    async with get_session() as session:
        experiment = ExperimentModel(name=f"exp-{run}")
        session.add(experiment)
        session.add(
            TaskModel(
                id=task_id,
                name=task_id,
                user="u",
                task_path="p",
                status=TaskStatus.COMPLETED,
                run_analysis=True,
            )
        )
        await session.flush()
        session.add(
            TrialModel(
                id=qa_id,
                name=qa_id,
                task_id=task_id,
                experiment_id=experiment.id,
                agent="claude-code",
                provider="local",
                queue_key="q",
                kind="qa",
                status=TrialStatus.SUCCESS,
                attempts=1,
                max_attempts=3,
            )
        )
        await session.commit()

    async with get_session() as session:
        with pytest.raises(HTTPException) as raised:
            await retry_trial_core(session, trial_id=qa_id)
        assert raised.value.status_code == 400
        assert "agent trials" in raised.value.detail


@pytest.mark.asyncio
async def test_historical_trials_do_not_block_the_qa_import():
    """Needs a database. QA admission is version-scoped, so the import
    staleness check must be too: a still-live trial on an old version must
    not defer the current version's settled QA result forever, while a
    live trial on the graded version still must."""
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from oddish.db import TaskStatus, TrialStatus, get_session, init_db
    from oddish.db.models import ExperimentModel, TaskModel, TaskVersionModel
    from oddish.workers.analysis_trials import _qa_import_still_current

    await init_db()
    run = uuid.uuid4().hex[:8]
    task_id = f"qa-version-scope-{run}"
    v1, v2 = f"{task_id}-v1", f"{task_id}-v2"
    async with get_session() as session:
        experiment = ExperimentModel(name=f"exp-{run}")
        session.add(experiment)
        session.add(
            TaskModel(
                id=task_id,
                name=task_id,
                user="u",
                task_path="p",
                status=TaskStatus.VERDICT_PENDING,
                run_analysis=True,
            )
        )
        await session.flush()
        for version_id, version in ((v1, 1), (v2, 2)):
            session.add(
                TaskVersionModel(
                    id=version_id, task_id=task_id, version=version, task_path="p"
                )
            )
        await session.flush()
        task = await session.get(TaskModel, task_id)
        task.current_version_id = v2
        for index, (version_id, status) in enumerate(
            ((v1, TrialStatus.RUNNING), (v2, TrialStatus.SUCCESS)), start=1
        ):
            session.add(
                TrialModel(
                    id=f"{task_id}-{index}",
                    name=f"{task_id}-{index}",
                    task_id=task_id,
                    task_version_id=version_id,
                    experiment_id=experiment.id,
                    agent="claude-code",
                    provider="local",
                    queue_key="q",
                    status=status,
                    attempts=1,
                    max_attempts=3,
                )
            )
        await session.commit()

    async with get_session() as session:
        # The v1 trial is live, but v2 is the graded version: import may land.
        assert await _qa_import_still_current(session, task_id, v2) is True
        # A live trial on the graded version itself still defers.
        trial = await session.get(TrialModel, f"{task_id}-2")
        trial.status = TrialStatus.RUNNING
        await session.flush()
        assert await _qa_import_still_current(session, task_id, v2) is False
        # Leave nothing for the sweep healer: this task has no experiment
        # membership, so a later test running the real cleanup sweep would
        # otherwise try (and fail) to create a QA trial for it.
        task = await session.get(TaskModel, task_id)
        task.status = TaskStatus.COMPLETED
        await session.commit()


@pytest.mark.asyncio
async def test_inplace_overwrite_cancels_the_overwritten_versions_audit():
    """Needs a database. In-place overwrite keeps the version id but
    replaces its bytes: the invalidator must cancel that version's live
    audit (or it keeps running against bytes that no longer exist) while
    leaving another version's audit alone."""
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from sqlalchemy import select

    from oddish.db import TaskStatus, TrialStatus, get_session, init_db
    from oddish.db.models import ExperimentModel, TaskModel, TaskVersionModel
    from oddish.queue import invalidate_task_qa_for_source_change

    await init_db()
    run = uuid.uuid4().hex[:8]
    task_id = f"qa-overwrite-{run}"
    v1, v2 = f"{task_id}-v1", f"{task_id}-v2"
    async with get_session() as session:
        experiment = ExperimentModel(name=f"exp-{run}")
        session.add(experiment)
        session.add(
            TaskModel(
                id=task_id,
                name=task_id,
                user="u",
                task_path="p",
                status=TaskStatus.RUNNING,
                run_analysis=True,
            )
        )
        await session.flush()
        for version_id, version in ((v1, 1), (v2, 2)):
            session.add(
                TaskVersionModel(
                    id=version_id, task_id=task_id, version=version, task_path="p"
                )
            )
        await session.flush()
        task = await session.get(TaskModel, task_id)
        task.current_version_id = v1
        for index, (version_id, kind) in enumerate(
            ((v1, "audit"), (v2, "audit"), (v1, "qa")), start=1
        ):
            session.add(
                TrialModel(
                    id=f"{task_id}-{index}",
                    name=f"{task_id}-{index}",
                    task_id=task_id,
                    task_version_id=version_id,
                    experiment_id=experiment.id,
                    agent="claude-code",
                    provider="local",
                    queue_key="q",
                    kind=kind,
                    status=TrialStatus.RUNNING,
                    attempts=1,
                    max_attempts=3,
                )
            )
        await session.commit()

    async with get_session() as session:
        task = (
            await session.execute(
                select(TaskModel).where(TaskModel.id == task_id).with_for_update()
            )
        ).scalar_one()
        await invalidate_task_qa_for_source_change(
            session, task, overwritten_version_id=v1
        )
        await session.commit()

    async with get_session() as session:
        overwritten_audit = await session.get(TrialModel, f"{task_id}-1")
        assert overwritten_audit.status == TrialStatus.FAILED
        assert overwritten_audit.harbor_stage == "cancelled"
        other_versions_audit = await session.get(TrialModel, f"{task_id}-2")
        assert other_versions_audit.status == TrialStatus.RUNNING
        qa = await session.get(TrialModel, f"{task_id}-3")
        assert qa.status == TrialStatus.FAILED
        assert qa.harbor_stage == "cancelled"


@pytest.mark.asyncio
async def test_a_stale_audit_never_imports_into_overwritten_bytes(monkeypatch):
    """Needs a database. The audit trial pins its version's content hash at
    creation; when the version's bytes changed underneath it (in-place
    overwrite racing a live audit), the import drops the findings instead
    of writing old-source results onto the new source."""
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from sqlalchemy import select, text

    from oddish.db import TaskStatus, TrialStatus, VerdictStatus, get_session, init_db
    from oddish.db.models import ExperimentModel, TaskModel, TaskVersionModel
    from oddish.workers import analysis_trials
    from oddish.workers.analysis_trials import (
        _import_audit_result,
        maybe_enqueue_audit_trial,
    )

    await init_db()
    run = uuid.uuid4().hex[:8]
    task_id = f"qa-audit-hash-{run}"
    version_id = f"{task_id}-v1"
    async with get_session() as session:
        experiment = ExperimentModel(name=f"exp-{run}")
        session.add(experiment)
        session.add(
            TaskModel(
                id=task_id,
                name=task_id,
                user="u",
                task_path="p",
                status=TaskStatus.RUNNING,
                run_analysis=True,
            )
        )
        await session.flush()
        await session.execute(
            text(
                "INSERT INTO task_experiments (task_id, experiment_id, created_at) "
                "VALUES (:t, :e, NOW())"
            ),
            {"t": task_id, "e": experiment.id},
        )
        session.add(
            TaskVersionModel(
                id=version_id,
                task_id=task_id,
                version=1,
                task_path="p",
                content_hash="original-bytes",
            )
        )
        await session.flush()
        task = await session.get(TaskModel, task_id)
        task.current_version_id = version_id
        assert await maybe_enqueue_audit_trial(
            session, task=task, task_version_id=version_id
        )
        await session.commit()

    async with get_session() as session:
        audit = (
            await session.execute(
                select(TrialModel).where(
                    TrialModel.task_id == task_id, TrialModel.kind == "audit"
                )
            )
        ).scalar_one()
        # Creation pinned the bytes it audits.
        pinned = audit.harbor_config["analysis_payload"]["task_version_content_hash"]
        assert pinned == "original-bytes"
        audit.status = TrialStatus.SUCCESS
        # Overwrite the version's bytes underneath the settled audit.
        version = await session.get(TaskVersionModel, version_id)
        version.content_hash = "overwritten-bytes"
        await session.commit()

    async def unexpected_read(trial, filename):
        raise AssertionError("a stale audit must not even read its artifact")

    monkeypatch.setattr(analysis_trials, "read_analysis_artifact", unexpected_read)
    await _import_audit_result(audit)
    async with get_session() as session:
        version = await session.get(TaskVersionModel, version_id)
        assert version.pre_trial is None
        assert version.pre_trial_status == VerdictStatus.QUEUED

    # With matching bytes the same import lands.
    async with get_session() as session:
        version = await session.get(TaskVersionModel, version_id)
        version.content_hash = "original-bytes"
        await session.commit()

    async def read_clean(trial, filename):
        return {"items": []}

    monkeypatch.setattr(analysis_trials, "read_analysis_artifact", read_clean)
    await _import_audit_result(audit)
    async with get_session() as session:
        version = await session.get(TaskVersionModel, version_id)
        assert version.pre_trial_status == VerdictStatus.SUCCESS
        assert version.pre_trial is not None


@pytest.mark.asyncio
async def test_cleanup_reimports_a_settled_audit(monkeypatch):
    """Needs a database. A settled audit whose importer died mid-write
    leaves its version stuck queued/running forever -- the settlement path
    promises the cleanup sweep re-runs importers, and this healer pass is
    what makes that true for audits (the QA healer only scans
    VERDICT_PENDING tasks)."""
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from sqlalchemy import select, text

    from oddish.db import TaskStatus, TrialStatus, VerdictStatus, get_session, init_db
    from oddish.db.models import ExperimentModel, TaskModel, TaskVersionModel
    from oddish.workers import analysis_trials
    from oddish.workers.analysis_trials import maybe_enqueue_audit_trial
    from oddish.workers.queue.cleanup import _heal_stale_audit_imports

    await init_db()
    run = uuid.uuid4().hex[:8]
    task_id = f"qa-audit-heal-{run}"
    version_id = f"{task_id}-v1"
    async with get_session() as session:
        experiment = ExperimentModel(name=f"exp-{run}")
        session.add(experiment)
        session.add(
            TaskModel(
                id=task_id,
                name=task_id,
                user="u",
                task_path="p",
                status=TaskStatus.RUNNING,
                run_analysis=True,
            )
        )
        await session.flush()
        await session.execute(
            text(
                "INSERT INTO task_experiments (task_id, experiment_id, created_at) "
                "VALUES (:t, :e, NOW())"
            ),
            {"t": task_id, "e": experiment.id},
        )
        session.add(
            TaskVersionModel(
                id=version_id,
                task_id=task_id,
                version=1,
                task_path="p",
                content_hash="bytes",
            )
        )
        await session.flush()
        task = await session.get(TaskModel, task_id)
        task.current_version_id = version_id
        assert await maybe_enqueue_audit_trial(
            session, task=task, task_version_id=version_id
        )
        await session.commit()

    # The audit settles, but no import ever lands: the wedged state.
    async with get_session() as session:
        audit = (
            await session.execute(
                select(TrialModel).where(
                    TrialModel.task_id == task_id, TrialModel.kind == "audit"
                )
            )
        ).scalar_one()
        audit.status = TrialStatus.SUCCESS
        await session.commit()
    async with get_session() as session:
        version = await session.get(TaskVersionModel, version_id)
        assert version.pre_trial_status == VerdictStatus.QUEUED

    async def read_clean(trial, filename):
        return {"items": []}

    monkeypatch.setattr(analysis_trials, "read_analysis_artifact", read_clean)
    # The sweep composes these two: the scan runs inside the sweep
    # transaction, the re-imports after it commits (the importer takes its
    # own locks).
    async with get_session() as session:
        stale = await _heal_stale_audit_imports(session)
    assert audit.id in stale
    from oddish.workers.analysis_trials import handle_analysis_trial_settled

    await handle_analysis_trial_settled(audit.id)

    async with get_session() as session:
        version = await session.get(TaskVersionModel, version_id)
        assert version.pre_trial_status == VerdictStatus.SUCCESS
        assert version.pre_trial is not None


@pytest.mark.asyncio
async def test_the_verdict_needs_enough_evidence():
    """Below 5 trials or 3 distinct agents the QA trial is created without a
    verdict request; at the bar it is asked for one."""

    class _Scalars:
        def __init__(self, agents):
            self._agents = agents

        def all(self):
            return self._agents

    class _Session:
        def __init__(self, agents):
            self._agents = agents

        async def scalars(self, _query):
            return _Scalars(self._agents)

    few = [f"t{i}" for i in range(4)]
    assert await has_verdict_evidence(_Session(["a", "b", "c", "d"]), few) is False

    five_two_agents = [f"t{i}" for i in range(5)]
    assert (
        await has_verdict_evidence(_Session(["a", "a", "b", "b", "a"]), five_two_agents)
        is False
    )

    five_three_agents = [f"t{i}" for i in range(5)]
    assert (
        await has_verdict_evidence(
            _Session(["a", "b", "c", "a", "b"]), five_three_agents
        )
        is True
    )


def test_the_verifier_actually_grades_the_artifact(tmp_path):
    """Run the generated tests/test.sh for real: only an artifact that
    covers exactly the requested trials with valid analyses earns reward
    1.0. An empty trials list, a subset, a missing file, or a missing
    verdict all fail. This is the whole retry mechanism, so it must work
    as a shell script, not just read well."""
    import json
    import subprocess

    from oddish.worker.probe_staging import apply_analysis_overlay

    apply_analysis_overlay(
        tmp_path,
        brief="b",
        artifact="qa_result.json",
        check_payload=_qa_check_payload(["t-1", "t-2"]),
    )
    test_sh = tmp_path / "tests" / "test.sh"

    def run(payload: str | None) -> int:
        logs = tmp_path / "logs"
        if logs.exists():
            import shutil

            shutil.rmtree(logs)
        logs.mkdir()
        if payload is not None:
            (logs / "qa_result.json").write_text(payload)
        out = logs / "verifier"
        result = subprocess.run(
            ["sh", str(test_sh)],
            env={"HARBOR_VERIFIER_LOG_DIR": str(out), "PATH": os.environ["PATH"]},
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )
        return result.returncode

    # The script reads the fixed path /logs/<artifact>; symlinking that
    # is not possible in a test, so rewrite the SRC line to the temp dir.
    test_sh.write_text(
        test_sh.read_text().replace(
            "/logs/qa_result.json", str(tmp_path / "logs" / "qa_result.json")
        )
    )

    good = {"trials": [_good_qa_entry("t-1"), _good_qa_entry("t-2")], "verdict": None}
    code = run(json.dumps(good))
    assert code == 0
    assert (tmp_path / "logs" / "verifier" / "reward.txt").read_text().strip() == "1.0"
    assert (tmp_path / "logs" / "verifier" / "qa_result.json").exists()

    # An empty result must NOT earn reward: the requested trials are absent.
    code = run(json.dumps({"trials": [], "verdict": None}))
    assert code == 1

    # A subset must not earn reward either.
    code = run(json.dumps({"trials": [_good_qa_entry("t-1")], "verdict": None}))
    assert code == 1

    code = run(json.dumps({}))
    assert code == 1

    code = run("not json")
    assert code == 1

    code = run(None)
    assert code == 1


def test_the_validator_requires_the_exact_trial_set():
    """Each requested trial exactly once: an empty, subset, padded, or
    duplicated artifact is invalid. This is what stops a partial result
    from publishing an incomplete verdict."""
    from oddish.worker.analysis_result_check import check_analysis_result

    expected = _qa_check_payload(["t-1", "t-2"])
    good = {"trials": [_good_qa_entry("t-1"), _good_qa_entry("t-2")], "verdict": None}
    assert check_analysis_result(good, expected) == []

    empty = {"trials": [], "verdict": None}
    assert any("missing entries" in e for e in check_analysis_result(empty, expected))
    subset = {"trials": [_good_qa_entry("t-1")], "verdict": None}
    assert any("missing entries" in e for e in check_analysis_result(subset, expected))
    padded = {
        "trials": [_good_qa_entry(t) for t in ("t-1", "t-2", "t-3")],
        "verdict": None,
    }
    assert any("unrequested" in e for e in check_analysis_result(padded, expected))
    doubled = {
        "trials": [_good_qa_entry(t) for t in ("t-1", "t-1", "t-2")],
        "verdict": None,
    }
    assert any("duplicate" in e for e in check_analysis_result(doubled, expected))
    assert check_analysis_result([], expected) == ["the artifact is not a JSON object"]


def test_the_validator_rejects_invalid_analyses_and_summaries():
    """A classification outside the taxonomy or a missing/empty trajectory
    summary must fail validation -- these were previously dropped or stored
    empty without failing anything."""
    from oddish.worker.analysis_result_check import check_analysis_result

    expected = _qa_check_payload(["t-1"])

    bad_label = _good_qa_entry("t-1")
    bad_label["analysis"] = dict(bad_label["analysis"], classification="NOT_A_LABEL")
    errors = check_analysis_result({"trials": [bad_label], "verdict": None}, expected)
    assert any("classification" in e for e in errors)

    no_summary = dict(_good_qa_entry("t-1"))
    del no_summary["trajectory_summary"]
    errors = check_analysis_result({"trials": [no_summary], "verdict": None}, expected)
    assert any("trajectory_summary" in e for e in errors)

    hollow = _good_qa_entry("t-1")
    hollow["trajectory_summary"] = dict(hollow["trajectory_summary"], components=[])
    errors = check_analysis_result({"trials": [hollow], "verdict": None}, expected)
    assert any("components" in e for e in errors)


def test_the_validator_enforces_the_verdict_contract():
    """A requested verdict must be a valid object; an unrequested one must
    be null, exactly as the brief instructs."""
    from oddish.worker.analysis_result_check import check_analysis_result

    with_verdict = _qa_check_payload(["t-1"], with_verdict=True)
    entry = _good_qa_entry("t-1")

    missing = {"trials": [entry], "verdict": None}
    assert any("verdict" in e for e in check_analysis_result(missing, with_verdict))
    valid = {
        "trials": [entry],
        "verdict": {"verdict": "accept", "confidence": "high"},
    }
    assert check_analysis_result(valid, with_verdict) == []
    wrong = {
        "trials": [entry],
        "verdict": {"verdict": "maybe", "confidence": "high"},
    }
    assert any("verdict" in e for e in check_analysis_result(wrong, with_verdict))

    without_verdict = _qa_check_payload(["t-1"])
    unasked = {
        "trials": [entry],
        "verdict": {"verdict": "accept", "confidence": "high"},
    }
    assert any("null" in e for e in check_analysis_result(unasked, without_verdict))


def test_the_validator_holds_audit_items_to_the_prompt_schema():
    """Every audit finding needs the ten keys with the exact values the
    prompt defines; the importer's tolerated alternate spellings (severity
    for tier, heading spellings for dimension) must pass too, so the
    verifier is never stricter than the importer."""
    from oddish.workers.analysis_trials import analysis_check_payload
    from oddish.worker.analysis_result_check import check_analysis_result

    expected = analysis_check_payload("audit", None)
    item = {
        "source": "pre_trial",
        "problem_type": "incompleteness",
        "dimension": "verifier",
        "file": "tests/verify.py",
        "line_start": 4,
        "line_end": 6,
        "title": "The verifier ignores the exit code",
        "detail": "It never asserts returncode.",
        "recommendation": "Assert returncode == 0.",
        "tier": "must_fix",
    }
    assert check_analysis_result({"items": []}, expected) == []
    assert check_analysis_result({"items": [item]}, expected) == []

    spelled = dict(item)
    spelled.pop("tier")
    spelled["severity"] = "must_fix"
    spelled["dimension"] = "verifier_completeness"
    assert check_analysis_result({"items": [spelled]}, expected) == []

    for key in ("source", "file", "line_start", "title", "detail"):
        broken = {k: v for k, v in item.items() if k != key}
        assert check_analysis_result({"items": [broken]}, expected), key
    assert check_analysis_result({"items": {}}, expected)


@pytest.mark.asyncio
async def test_the_qa_import_is_all_or_nothing(monkeypatch):
    """Needs a database. An artifact that fails the shared validator (here:
    grading only a subset of the requested trials) must import nothing --
    no per-trial grades, a recorded verdict error -- while a valid artifact
    grades every row."""
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from oddish.db import (
        AnalysisStatus,
        TaskStatus,
        TrialStatus,
        VerdictStatus,
        get_session,
        init_db,
    )
    from oddish.db.models import ExperimentModel, TaskModel
    from oddish.workers import analysis_trials
    from oddish.workers.analysis_trials import _import_qa_result

    await init_db()
    run = uuid.uuid4().hex[:8]
    task_id = f"qa-atomic-{run}"
    graded_ids = [f"{task_id}-1", f"{task_id}-2"]
    qa_id = f"{task_id}-3"
    async with get_session() as session:
        experiment = ExperimentModel(name=f"exp-{run}")
        session.add(experiment)
        session.add(
            TaskModel(
                id=task_id,
                name=task_id,
                user="u",
                task_path="p",
                status=TaskStatus.VERDICT_PENDING,
                run_analysis=True,
            )
        )
        await session.flush()
        for trial_id in graded_ids:
            session.add(
                TrialModel(
                    id=trial_id,
                    name=trial_id,
                    task_id=task_id,
                    experiment_id=experiment.id,
                    agent="claude-code",
                    provider="local",
                    queue_key="q",
                    status=TrialStatus.SUCCESS,
                    attempts=1,
                    max_attempts=3,
                )
            )
        session.add(
            TrialModel(
                id=qa_id,
                name=qa_id,
                task_id=task_id,
                experiment_id=experiment.id,
                agent="claude-code",
                provider="local",
                queue_key="q",
                kind="qa",
                status=TrialStatus.SUCCESS,
                attempts=1,
                max_attempts=3,
                harbor_config={
                    "mode": "qa",
                    "analysis_payload": {
                        "trial_ids": graded_ids,
                        "with_verdict": False,
                    },
                },
            )
        )
        await session.commit()

    async def no_trajectory(row):
        return None

    monkeypatch.setattr("oddish.core.trial_io.read_trial_trajectory", no_trajectory)

    subset = {"trials": [_good_qa_entry(graded_ids[0])], "verdict": None}

    async def read_subset(trial, filename):
        return subset

    monkeypatch.setattr(analysis_trials, "read_analysis_artifact", read_subset)
    async with get_session() as session:
        qa_trial = await session.get(TrialModel, qa_id)
    await _import_qa_result(qa_trial)

    async with get_session() as session:
        for trial_id in graded_ids:
            row = await session.get(TrialModel, trial_id)
            assert row.analysis is None, "a partial artifact must store nothing"
        task = await session.get(TaskModel, task_id)
        assert task.verdict_status == VerdictStatus.FAILED
        assert "violates the QA contract" in (task.verdict_error or "")
        # Re-arm so the second import may store its state.
        task.status = TaskStatus.VERDICT_PENDING
        task.verdict_status = VerdictStatus.QUEUED
        task.verdict_error = None
        await session.commit()

    complete = {
        "trials": [_good_qa_entry(t) for t in graded_ids],
        "verdict": None,
    }

    async def read_complete(trial, filename):
        return complete

    monkeypatch.setattr(analysis_trials, "read_analysis_artifact", read_complete)
    await _import_qa_result(qa_trial)

    async with get_session() as session:
        for trial_id in graded_ids:
            row = await session.get(TrialModel, trial_id)
            assert row.analysis is not None
            assert row.analysis["_graded_by"] == qa_id
            assert row.analysis_status == AnalysisStatus.SUCCESS
            assert row.trajectory_summary["components"], trial_id
        task = await session.get(TaskModel, task_id)
        assert task.status == TaskStatus.COMPLETED


def test_the_importer_stamps_derived_facts_onto_the_summary():
    """tool_count / duration / subagent dispatches / provenance are counted
    from the trajectory by the importer, never taken from the model (#1275),
    and the version stamps match what freshness comparisons key on."""
    from oddish.analyze.trajectory_taxonomy import SCHEMA_VERSION, taxonomy_version
    from oddish.workers.analysis_trials import enrich_trajectory_summary

    trajectory = {
        "agent": "claude-code",
        "steps": [
            {
                "step_id": 1,
                "timestamp": "2026-08-18T00:00:00Z",
                "tool_calls": [
                    {"name": "Write", "arguments": {"file_path": "/app/x.py"}}
                ],
            },
            {
                "step_id": 2,
                "timestamp": "2026-08-18T00:00:05Z",
                "tool_calls": [
                    {"name": "Edit", "arguments": {"file_path": "/app/x.py"}},
                    {"name": "Agent", "arguments": {"prompt": "go"}},
                ],
            },
        ],
    }
    summary = {
        "summary": "s",
        "highlights": [],
        "components": [
            {"step_ids": [1, 2], "trajectory_component": "implementing", "summary": "c"}
        ],
    }
    out = enrich_trajectory_summary(
        summary, trajectory=trajectory, model="fireworks/glm-5p2", graded_by="t-9"
    )
    assert out["schema_version"] == SCHEMA_VERSION
    assert out["taxonomy_version"] == taxonomy_version()
    assert out["model"] == "fireworks/glm-5p2"
    assert out["_graded_by"] == "t-9"
    component = out["components"][0]
    assert component["tool_count"] == 3
    assert component["duration_ms"] == 5000
    assert component["subagent_dispatches"] == 1
    # Step 2 edits the path step 1 authored: counted, not judged.
    assert component["provenance_capable"] is True
    assert component["revisits_own_edits"] is True


@pytest.mark.asyncio
async def test_no_analysis_trial_is_created_for_a_deleted_task():
    """Needs a database. A tombstoned task must never get analysis spend."""
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from oddish.db import TaskStatus, get_session, init_db, utcnow
    from oddish.db.models import TaskModel
    from oddish.workers.analysis_trials import create_analysis_trial

    await init_db()
    run = uuid.uuid4().hex[:8]
    task_id = f"tombstone-{run}"
    async with get_session() as session:
        session.add(
            TaskModel(
                id=task_id,
                name=task_id,
                user="u",
                task_path="p",
                status=TaskStatus.COMPLETED,
                run_analysis=True,
                deleted_at=utcnow(),
            )
        )
        await session.commit()

    async with get_session() as session:
        task = await session.get(
            TaskModel, task_id, execution_options={"include_deleted": True}
        )
        with pytest.raises(RuntimeError, match="deleted task"):
            await create_analysis_trial(session, task=task, kind="audit", brief="b")


def test_only_probe_trials_get_the_inline_probe_summary():
    """qa/audit trials carry extra_instructions (their brief) exactly like
    probes do, but their analysis IS the trial: the direct probe analyzer
    must not also run for them. It would be a second, unintended LLM call
    per analysis run, and it would stamp probe-style analysis fields onto
    the qa/audit row."""
    from oddish.workers.queue.trial_handler import (
        should_generate_inline_probe_summary,
    )

    for mode in ("qa", "audit"):
        assert should_generate_inline_probe_summary(mode, "the brief") is False
    assert should_generate_inline_probe_summary(None, "probe instructions") is True
    assert should_generate_inline_probe_summary("probe", "probe instructions") is True
    assert should_generate_inline_probe_summary(None, None) is False
    assert should_generate_inline_probe_summary(None, "") is False


def test_the_view_definition_cannot_drift_between_fresh_and_migrated_dbs():
    """The analysis_spend view is created two ways: migration
    ``analysisspend01`` on migrated databases, the models' ``after_create``
    listener on create_all databases. If someone edits one and not the
    other, fresh and prod databases silently serve different cost numbers."""
    import re
    from pathlib import Path

    from oddish.db.models import ANALYSIS_SPEND_VIEW_SQL

    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "analysisspend01_create_analysis_spend_view.py"
    ).read_text()
    match = re.search(r'op\.execute\(\s*"""(.*?)"""', migration, flags=re.S)
    assert match, "the migration no longer holds an inline view definition"
    normalize = lambda sql: re.sub(r"\s+", " ", sql).strip()  # noqa: E731
    assert normalize(match.group(1)) == normalize(ANALYSIS_SPEND_VIEW_SQL)
