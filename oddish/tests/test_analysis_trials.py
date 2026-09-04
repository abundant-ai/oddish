"""Tests for analysis trials.

Each test checks one rule. The rule is in the test name and the first line.
"""

import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from oddish.analyze.models import TaskVerdictModel
from oddish.core.verdict_sync import apply_deterministic_verdict_rules
from oddish.db.models import TrialModel
from oddish.filters.trial_predicates import EligibleTrialScope
from oddish.core.trial_facets import facet_rows_for_trial
from oddish.workers.analysis_trials import (
    _classification_from_analysis,
    audit_policy_hash,
    build_audit_brief,
    build_qa_brief,
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

LEGACY_CLASSIFIER_TOKENS = {
    "{result}",
    "{trial_agent_context}",
    "{task_dir}",
    "{trial_dir}",
    "{pre_trial_context}",
    "{file_access_context}",
    "{trajectory_components_context}",
}


def test_the_analysis_kinds_are_known():
    """qa, qa_eval, audit, and summarize are analysis kinds. agent is not."""
    for kind in ("qa", "qa_eval", "audit", "summarize"):
        assert is_analysis_kind(kind)
    assert not is_analysis_kind("agent")
    assert not is_analysis_kind(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", [None, "modal", "daytona"])
async def test_analysis_creation_stores_kind_only_once(monkeypatch, environment):
    from oddish.db import TaskModel
    from oddish.workers.analysis_trials import create_analysis_trial

    task = TaskModel(
        id="task-1",
        name="task-1",
        org_id="org-1",
        current_version_id=None,
    )

    class FakeSession:
        def __init__(self):
            self.added = None

        def add(self, row):
            self.added = row

        async def flush(self):
            return None

    async def reserve_next_trial_index(_session, *, task_id):
        assert task_id == "task-1"
        return 4

    async def enqueue_trial_worker_job(_session, **_kwargs):
        return None

    monkeypatch.setattr(
        "oddish.queue.reserve_next_trial_index", reserve_next_trial_index
    )
    monkeypatch.setattr(
        "oddish.queue.enqueue_trial_worker_job", enqueue_trial_worker_job
    )
    session = FakeSession()

    trial = await create_analysis_trial(
        session,
        task=task,
        kind="qa",
        brief="grade the trials",
        payload={"trial_ids": ["source-1"]},
        experiment_id="analysis-experiment",
        environment=environment,
    )

    assert trial.kind == "qa"
    assert trial.environment == environment
    assert "mode" not in trial.harbor_config
    assert trial.harbor_config["analysis_payload"]["trial_ids"] == ["source-1"]


@pytest.mark.asyncio
async def test_audit_creation_pins_policy_without_a_content_hash(
    monkeypatch,
):
    from types import SimpleNamespace

    from oddish.db import TaskModel, TaskVersionModel
    from oddish.workers.analysis_trials import create_analysis_trial

    task = TaskModel(
        id="task-1",
        name="task-1",
        org_id="org-1",
        current_version_id="version-1",
    )
    version = SimpleNamespace(id="version-1", content_hash=None)

    class FakeSession:
        def add(self, _row):
            return None

        async def flush(self):
            return None

        async def get(self, model, row_id):
            if model is TaskVersionModel and row_id == version.id:
                return version
            return None

    async def reserve_next_trial_index(_session, *, task_id):
        assert task_id == "task-1"
        return 1

    async def enqueue_trial_worker_job(_session, **_kwargs):
        return None

    monkeypatch.setattr(
        "oddish.queue.reserve_next_trial_index", reserve_next_trial_index
    )
    monkeypatch.setattr(
        "oddish.queue.enqueue_trial_worker_job", enqueue_trial_worker_job
    )

    trial = await create_analysis_trial(
        FakeSession(),
        task=task,
        kind="audit",
        brief="audit this version",
        task_version_id=version.id,
        experiment_id="analysis-experiment",
    )

    assert trial.harbor_config["analysis_payload"] == {
        "audit_policy_hash": audit_policy_hash()
    }


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
    assert "/tmp/qa_result-draft.json" in brief
    assert "/probe-harness/submit-analysis-result" in brief
    assert '`action_items[].problem_type` accepts only `"incompleteness"' in brief
    assert "If `exploited` is `false`, `causal` must also be `false`" in brief
    assert '"evidence": ["first fact", "second fact"]' in brief
    assert "oddish-query task fetch --into /tmp/qa-task-source" in brief
    assert "`/tmp/qa-task-source/instruction.md`" in brief
    assert "`/tmp/qa-task-source/task.toml`" in brief
    assert "does not prove that the historical solver could read hidden tests" in brief


def test_the_production_classifier_uses_the_query_evidence_contract():
    """The packaged policy must describe evidence QA actually receives. It
    must not retain placeholders or paths from the deleted mounted-dir flow."""
    brief = build_qa_brief(
        task_name="demo",
        trial_ids=["t-1"],
        pre_trial_items=None,
        with_verdict=False,
    )

    for token in LEGACY_CLASSIFIER_TOKENS:
        assert token not in brief
    assert "/tmp/<trial-id>.result.json" in brief
    assert "/tmp/<trial-id>.verifier.json" in brief
    assert "/tmp/<trial-id>.trajectory.json" in brief
    assert "final workspace" in brief
    assert "not mounted in the QA sandbox" in brief
    assert "stop without writing `qa_result.json`" in brief
    assert "Missing QA evidence is not a solver HARNESS_ERROR" in brief
    assert "An empty verifier `stdout` or `stderr` string" in brief
    assert "`stdout`, `stderr`, and `exception` are all null" in brief
    assert "The fetched task source is QA-only evidence" in brief
    assert "The exact exception `AgentTimeoutError`" in brief
    assert "it is not HARNESS_ERROR" in brief
    assert "check every failed verifier condition" in brief
    assert "leaving the agent's final work unchanged" in brief
    assert "The first verifier error is not enough" in brief
    assert "skipped by an earlier build or compile failure" in brief
    assert "one logical stream across all write calls" in brief
    assert "write-call boundaries, `lastCall`" in brief
    assert "found only in the verifier, hidden tests, or reference solution" in brief
    assert "Do not let an unrelated task defect change" in brief
    assert "Oracle copying or other proven unintended access is BAD_SUCCESS" in brief
    assert "`action_items[].causal` is required" in brief
    assert "whether or not the defect decided this run" not in brief
    for ambiguous in (
        "inferable",
        "strongest available evidence",
        "first decisive finding",
        "Complete the infrastructure",
        "visible code contract",
        "when it is needed",
    ):
        assert ambiguous not in brief
    assert brief.count("== OUTPUT ==") == 1
    for token in (
        "{num_trials}",
        "{baseline_summary}",
        "{quality_check_summary}",
        "{trial_classifications}",
        "{{",
        "}}",
    ):
        assert token not in brief


def test_the_qa_brief_distinguishes_a_clean_audit_from_an_audit_failure():
    failed = build_qa_brief(
        task_name="demo",
        trial_ids=["t-1"],
        pre_trial_items=None,
        with_verdict=False,
        trial_evidence=[
            {
                "trial_id": "t-1",
                "status": "failed",
                "reward": None,
                "has_trajectory": False,
                "agent": "codex",
                "baseline_kind": None,
            }
        ],
        baseline_evidence=[
            {
                "trial_id": "nop-1",
                "status": "success",
                "reward": 1.0,
                "has_trajectory": True,
                "agent": "nop",
                "baseline_kind": "nop",
            }
        ],
        pre_trial_status="failed",
        pre_trial_error="audit_result.json was missing",
    )

    assert "Source-audit status: failed" in failed
    assert "Source-audit error: audit_result.json was missing" in failed
    assert '"baseline_kind": "nop"' in failed
    assert '"has_trajectory": false' in failed

    clean = build_qa_brief(
        task_name="demo",
        trial_ids=["t-1"],
        pre_trial_items=None,
        with_verdict=False,
        pre_trial_status="success",
    )
    assert "Source-audit status: success" in clean
    assert "Source-audit error: (none)" in clean


def test_the_audit_brief_names_its_output_file():
    """The audit agent must know where to write, and must not solve the task."""
    brief = build_audit_brief(task_name="demo")
    assert "audit_result.json" in brief
    assert "Do not solve the task" in brief


def test_the_audit_policy_covers_model_tuning_and_binary_bypasses():
    brief = build_audit_brief(task_name="demo")

    assert "Model-conditioned grading is a verifier defect" in brief
    assert "pass@k" in brief
    assert "Treat authoring instructions and generated settings the same" in brief
    assert "Do not report a fixed task only because every tested model fails" in brief
    assert "decrypts, copies, or runs a bundled implementation" in brief
    assert "verifier must still build and test the requested replacement" in brief
    assert "hidden binary that only supplies expected outputs" in brief


def test_the_no_verdict_brief_does_not_contradict_itself():
    """with_verdict=False must not show the verdict-object template or its
    schema: the strict verifier requires null there, and a template
    contradicting the prose would burn every retry attempt."""
    brief = build_qa_brief(
        task_name="demo",
        trial_ids=["t-1"],
        pre_trial_items=None,
        with_verdict=False,
    )
    assert '"verdict": null' in brief
    assert "Verdict JSON schema" not in brief
    assert "<object matching this JSON schema>" not in brief
    assert "trials result <trial-id>" in brief
    assert "trials trajectory <trial-id>" in brief
    assert "> /tmp/<trial-id>.result.json" in brief
    assert "> /tmp/<trial-id>.verifier.json" in brief
    assert "> /tmp/<trial-id>.trajectory.json" in brief
    assert brief.count("/tmp/<trial-id>.result.json") == 1
    assert brief.count("/tmp/<trial-id>.verifier.json") == 1
    assert brief.count("/tmp/<trial-id>.trajectory.json") == 1
    assert brief.count("Missing QA evidence is not a solver HARNESS_ERROR") == 1
    assert "only when diagnosing a setup or runtime failure" in brief
    assert "do not infer agent behavior" in brief


def _qa_check_payload(
    trial_ids: list[str],
    *,
    with_verdict: bool = False,
    trial_evidence: list[dict] | None = None,
    pre_trial_item_ids: list[str] | None = None,
    pre_trial_must_fix_ids: list[str] | None = None,
) -> dict:
    from oddish.workers.analysis_trials import analysis_check_payload

    return analysis_check_payload(
        "qa",
        {
            "analysis_payload": {
                "trial_ids": trial_ids,
                "trial_evidence": trial_evidence or [],
                "pre_trial_item_ids": pre_trial_item_ids or [],
                "pre_trial_must_fix_ids": pre_trial_must_fix_ids or [],
                "with_verdict": with_verdict,
            }
        },
    )


def test_qa_eval_check_payload_requires_exactly_one_source_trial():
    from oddish.core.analysis_payload import AnalysisPayloadError
    from oddish.workers.analysis_trials import analysis_check_payload

    with pytest.raises(AnalysisPayloadError, match="exactly one source trial"):
        analysis_check_payload(
            "qa_eval",
            {"analysis_payload": {"trial_ids": ["source-1", "source-2"]}},
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        ({"trial_ids": []}, "must not be empty"),
        ({"trial_ids": ["source-1", "source-1"]}, "must not contain duplicates"),
        (
            {
                "trial_ids": ["source-1"],
                "trial_evidence": [{"trial_id": "source-2"}],
            },
            "must cover trial_ids exactly",
        ),
        (
            {
                "trial_ids": ["source-1"],
                "pre_trial_item_ids": ["audit-1"],
                "pre_trial_must_fix_ids": ["audit-2"],
            },
            "must be a subset",
        ),
        ({"trial_ids": ["source-1"], "with_verdict": "yes"}, "must be a boolean"),
    ),
)
def test_qa_payload_parser_rejects_inconsistent_persisted_state(payload, message):
    from oddish.core.analysis_payload import AnalysisPayloadError
    from oddish.workers.analysis_trials import analysis_check_payload

    with pytest.raises(AnalysisPayloadError, match=message):
        analysis_check_payload("qa", {"analysis_payload": payload})


def test_analysis_check_payload_rejects_unknown_kinds():
    from oddish.core.analysis_payload import AnalysisPayloadError
    from oddish.workers.analysis_trials import analysis_check_payload

    with pytest.raises(AnalysisPayloadError, match="unsupported"):
        analysis_check_payload("typo", {"analysis_payload": {}})


def test_audit_payload_allows_historical_missing_metadata_and_parses_hashes():
    from oddish.core.analysis_payload import parse_analysis_payload
    from oddish.workers.analysis_trials import analysis_check_payload

    expected = analysis_check_payload(
        "audit", {"extra_instructions": "audit this version"}
    )
    assert expected["kind"] == "audit"

    parsed = parse_analysis_payload(
        "audit",
        {
            "analysis_payload": {
                "task_version_content_hash": "  sha256:current  ",
                "audit_policy_hash": "a" * 64,
            }
        },
    )
    assert parsed.task_version_content_hash == "sha256:current"
    assert parsed.audit_policy_hash == "a" * 64


def test_audit_payload_rejects_malformed_content_hashes():
    from oddish.core.analysis_payload import AnalysisPayloadError
    from oddish.workers.analysis_trials import analysis_check_payload

    with pytest.raises(AnalysisPayloadError, match="non-empty string"):
        analysis_check_payload(
            "audit",
            {"analysis_payload": {"task_version_content_hash": ""}},
        )

    with pytest.raises(AnalysisPayloadError, match="audit_policy_hash"):
        analysis_check_payload(
            "audit",
            {"analysis_payload": {"audit_policy_hash": "not-a-sha256"}},
        )


@pytest.mark.asyncio
async def test_malformed_qa_payload_enters_the_verdict_failure_path(monkeypatch):
    from types import SimpleNamespace

    from oddish.db import TrialStatus
    from oddish.workers import analysis_trials

    captured = {}

    async def unexpected_artifact_read(*_args):
        raise AssertionError("an invalid persisted payload must fail before S3 access")

    async def capture_failure(task_id, *, payload, should_store, error):
        captured.update(task_id=task_id, payload=payload, error=error)

    monkeypatch.setattr(
        analysis_trials, "read_analysis_artifact", unexpected_artifact_read
    )
    monkeypatch.setattr(analysis_trials, "sync_verdict_to_task", capture_failure)

    await analysis_trials._import_qa_result(
        SimpleNamespace(
            id="qa-1",
            task_id="task-1",
            task_version_id="version-1",
            harbor_config={"analysis_payload": {"trial_ids": []}},
            status=TrialStatus.SUCCESS,
        )
    )

    assert captured["task_id"] == "task-1"
    assert captured["payload"] is None
    assert "must not be empty" in captured["error"]


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


@pytest.mark.asyncio
async def test_qa_creation_persists_the_pre_trial_contract(monkeypatch):
    from oddish.db import TaskVersionModel, TrialStatus, VerdictStatus
    from oddish.worker.analysis_result_check import check_analysis_result
    from oddish.workers.analysis_trials import analysis_check_payload, create_qa_trial

    task = SimpleNamespace(
        id="task-1",
        name="demo",
        current_version_id="version-1",
    )
    version = SimpleNamespace(
        id="version-1",
        content_hash="source-hash",
        pre_trial_started_at=None,
        pre_trial_finished_at=None,
        pre_trial={
            "items": [
                {"id": "audit-1", "tier": "must_fix"},
                {"id": "audit-2", "severity": "should_fix"},
                {"id": "audit-1", "tier": "must_fix"},
                {"title": "An old finding without an id"},
            ]
        },
        pre_trial_status=VerdictStatus.SUCCESS,
        pre_trial_error=None,
    )
    source = SimpleNamespace(
        id="trial-1",
        status=TrialStatus.SUCCESS,
        reward=0.0,
        has_trajectory=True,
        agent="codex",
    )

    class Result:
        def scalars(self):
            return self

        def all(self):
            return [source]

    class Session:
        async def get(self, model, row_id):
            assert model is TaskVersionModel
            assert row_id == "version-1"
            return version

        async def execute(self, _statement):
            return Result()

    captured = {}

    async def fake_create_analysis_trial(_session, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id="qa-1")

    monkeypatch.setattr(
        "oddish.workers.analysis_trials.create_analysis_trial",
        fake_create_analysis_trial,
    )

    await create_qa_trial(
        Session(),
        task=task,
        eligible_trial_ids=["trial-1"],
        with_verdict=False,
    )

    payload = captured["payload"]
    assert payload["pre_trial_item_ids"] == ["audit-1", "audit-2"]
    assert payload["pre_trial_must_fix_ids"] == ["audit-1"]
    assert payload["trial_evidence"] == [
        {
            "trial_id": "trial-1",
            "status": "success",
            "reward": 0.0,
            "has_trajectory": True,
            "agent": "codex",
            "baseline_kind": None,
        }
    ]
    assert payload["baseline_evidence"] == []

    entry = _good_qa_entry("trial-1")
    entry["analysis"]["exploitation"] = [
        {
            "links_to": item_id,
            "exploited": False,
            "exploit_evidence": None,
            "causal": False,
        }
        for item_id in payload["pre_trial_item_ids"]
    ]
    expected = analysis_check_payload("qa", {"analysis_payload": payload})
    assert check_analysis_result({"trials": [entry], "verdict": None}, expected) == []


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
    submit = tmp_path / "submit-analysis-result"
    assert submit.stat().st_mode & 0o111
    assert "analysis_result_check.py" in submit.read_text()
    assert (
        json.loads((tmp_path / ".analysis-contract" / "expected.json").read_text())
        == payload
    )


def test_submission_helper_rejects_then_publishes_a_repaired_qa_result(tmp_path):
    import json
    import os
    import subprocess

    from oddish.worker.probe_staging import apply_analysis_overlay, stage_cli_mount

    task = tmp_path / "task"
    task.mkdir()
    payload = _qa_check_payload(["t-1"])
    apply_analysis_overlay(
        task, brief="the brief", artifact="qa_result.json", check_payload=payload
    )
    harness = tmp_path / "harness"
    stage_cli_mount(harness, analysis_task_dir=task)
    logs = tmp_path / "logs"
    attempts = tmp_path / "attempts"
    draft = tmp_path / "draft.json"
    draft.write_text(json.dumps({"trials": [], "verdict": None}))
    env = {
        **os.environ,
        "ODDISH_ANALYSIS_LOG_DIR": str(logs),
        "ODDISH_ANALYSIS_ATTEMPTS_FILE": str(attempts),
    }

    rejected = subprocess.run(
        [str(harness / "submit-analysis-result"), str(draft)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode == 1
    assert "QA artifact validation failed (submission 1 of 3)" in rejected.stderr
    assert "missing entries for requested trials: ['t-1']" in rejected.stderr
    assert not (logs / "qa_result.json").exists()
    assert json.loads((logs / "qa_result-rejected.json").read_text()) == {
        "trials": [],
        "verdict": None,
    }

    draft.write_text(json.dumps({"trials": [_good_qa_entry("t-1")], "verdict": None}))
    accepted = subprocess.run(
        [str(harness / "submit-analysis-result"), str(draft)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert accepted.returncode == 0, accepted.stderr
    assert "accepted and published" in accepted.stdout
    assert json.loads((logs / "qa_result.json").read_text()) == json.loads(
        draft.read_text()
    )
    assert not (logs / "qa_submission_error.txt").exists()
    assert not (logs / "qa_result-rejected.json").exists()


def test_the_single_llm_overlay_does_not_install_the_query_cli(tmp_path):
    from oddish.worker.probe_staging import apply_analysis_overlay

    apply_analysis_overlay(
        tmp_path,
        brief="bounded prompt",
        artifact="summary_result.json",
        check_payload={"kind": "summarize", "target_trial_id": "t-42"},
        needs_query_cli=False,
    )

    dockerfile = (tmp_path / "environment" / "Dockerfile").read_text()
    assert "python:3.13-slim" in dockerfile
    assert "apt-get" not in dockerfile
    assert "nodejs" not in dockerfile


def test_a_correct_analysis_is_accepted():
    """A well-formed analysis from the QA agent parses into a classification."""
    parsed = _classification_from_analysis(GOOD_ANALYSIS, trial_name="t-1", reward=0.0)
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
    assert _classification_from_analysis(broken, trial_name="t-1", reward=0.0) is None


def test_a_must_fix_source_audit_overrides_an_accept_verdict():
    accepted = TaskVerdictModel(verdict="accept", confidence="high")

    rejected = apply_deterministic_verdict_rules(
        accepted, must_fix_ids=["finding-1"], baseline_evidence=[]
    )

    assert rejected.verdict == "reject"
    assert rejected.confidence == "high"
    assert "1 must-fix finding" in rejected.primary_issue
    assert (
        apply_deterministic_verdict_rules(
            accepted, must_fix_ids=[], baseline_evidence=[]
        )
        is accepted
    )


def test_a_failed_deterministic_baseline_overrides_an_accept_verdict():
    accepted = TaskVerdictModel(verdict="accept", confidence="high")

    rejected = apply_deterministic_verdict_rules(
        accepted,
        must_fix_ids=[],
        baseline_evidence=[{"agent": "nop", "reward": 1.0}],
    )

    assert rejected.verdict == "reject"
    assert rejected.primary_issue.startswith("CRITICAL:")


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
        assert "mode" not in qa_trials[0].harbor_config
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
        pinned_policy = audit.harbor_config["analysis_payload"]["audit_policy_hash"]
        assert pinned_policy == audit_policy_hash()
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
        assert version.pre_trial["audit_policy_hash"] == pinned_policy


@pytest.mark.asyncio
async def test_pre_trial_write_rechecks_the_hash_under_the_lock():
    """Needs a database. The audit import's early hash check runs unlocked,
    so an in-place overwrite can commit between it and the write. The write
    itself must re-check the pin under the version row lock and skip -- or
    old-bytes findings land as SUCCESS on the new bytes and block the
    None->QUEUED CAS that would audit them."""
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from oddish.core.verdict_sync import sync_pre_trial_to_task_version
    from oddish.db import VerdictStatus, get_session, init_db
    from oddish.db.models import TaskModel, TaskVersionModel

    await init_db()
    run = uuid.uuid4().hex[:8]
    task_id = f"qa-hash-lock-{run}"
    version_id = f"{task_id}-v1"
    async with get_session() as session:
        session.add(
            TaskModel(
                id=task_id, name=task_id, user="u", task_path="p", run_analysis=True
            )
        )
        await session.flush()
        session.add(
            TaskVersionModel(
                id=version_id,
                task_id=task_id,
                version=1,
                task_path="p",
                content_hash="new-bytes",
            )
        )
        await session.commit()

    stale = await sync_pre_trial_to_task_version(
        version_id,
        payload={"items": []},
        error=None,
        expected_content_hash="old-bytes",
    )
    assert stale is None
    async with get_session() as session:
        version = await session.get(TaskVersionModel, version_id)
        assert version.pre_trial is None
        assert version.pre_trial_status is None

    current = await sync_pre_trial_to_task_version(
        version_id,
        payload={"items": []},
        error=None,
        expected_content_hash="new-bytes",
    )
    assert current == VerdictStatus.SUCCESS.value
    async with get_session() as session:
        version = await session.get(TaskVersionModel, version_id)
        assert version.pre_trial_status == VerdictStatus.SUCCESS


@pytest.mark.asyncio
async def test_cancelling_qa_in_the_deferral_window_settles_the_task():
    """Needs a database. With every agent trial settled and the audit still
    live, admission holds the task in RUNNING. Cancelling QA there must
    settle the task too: left RUNNING, the sweep's advance backstop would
    re-enter admission minutes later and start a QA run the user just
    cancelled, against a brief whose audit findings the cancel wiped."""
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from oddish.core.endpoints.qa import cancel_task_qa_core
    from oddish.db import TaskStatus, TrialStatus, get_session, init_db
    from oddish.db.models import ExperimentModel, TaskModel

    await init_db()
    run = uuid.uuid4().hex[:8]
    task_id = f"qa-cancel-window-{run}"
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
        for trial_id, kind, status in (
            (agent_id, "agent", TrialStatus.SUCCESS),
            (audit_id, "audit", TrialStatus.RUNNING),
        ):
            session.add(
                TrialModel(
                    id=trial_id,
                    name=trial_id,
                    task_id=task_id,
                    experiment_id=experiment.id,
                    agent="claude-code",
                    provider="local",
                    queue_key="q",
                    kind=kind,
                    status=status,
                    attempts=1,
                    max_attempts=3,
                )
            )
        await session.commit()

    async with get_session() as session:
        await cancel_task_qa_core(session, task_id=task_id)
        await session.commit()

    async with get_session() as session:
        audit = await session.get(TrialModel, audit_id)
        assert audit.status == TrialStatus.FAILED
        assert audit.harbor_stage == "cancelled"
        task = await session.get(TaskModel, task_id)
        assert task.status == TaskStatus.FAILED
        assert task.finished_at is not None


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
        audit.has_trajectory = True
        await session.commit()
    async with get_session() as session:
        version = await session.get(TaskVersionModel, version_id)
        assert version.pre_trial_status == VerdictStatus.QUEUED

    async def read_clean(trial, filename):
        return {"items": []}

    monkeypatch.setattr(analysis_trials, "read_analysis_artifact", read_clean)

    async def read_audit_trajectory(trial):
        return {
            "steps": [
                {
                    "step_id": 1,
                    "tool_calls": [
                        {
                            "name": "Write",
                            "arguments": {"absolute_path": "/logs/audit_result.json"},
                        }
                    ],
                }
            ]
        }

    monkeypatch.setattr(
        "oddish.core.trial_io.read_trial_trajectory", read_audit_trajectory
    )
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
        audit_row = await session.get(TrialModel, audit.id)
        assert audit_row.trajectory_summary["generator"] == "analysis-activity"
        assert (
            audit_row.trajectory_summary["taxonomy_version"]
            == analysis_trials.ANALYSIS_ACTIVITY_VERSION
        )
        assert "wrote audit_result.json" in audit_row.trajectory_summary["summary"]


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

    def run(payload: str | None) -> tuple[int, Path]:
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
        return result.returncode, out

    # The script reads the fixed path /logs/<artifact>; symlinking that
    # is not possible in a test, so rewrite the SRC line to the temp dir.
    test_sh.write_text(
        test_sh.read_text().replace(
            "/logs/qa_result.json", str(tmp_path / "logs" / "qa_result.json")
        )
    )

    good = {"trials": [_good_qa_entry("t-1"), _good_qa_entry("t-2")], "verdict": None}
    code, out = run(json.dumps(good))
    assert code == 0
    assert (out / "reward.txt").read_text().strip() == "1.0"
    assert (out / "qa_result.json").exists()

    # An empty result must NOT earn reward: the requested trials are absent.
    code, out = run(json.dumps({"trials": [], "verdict": None}))
    assert code == 1
    assert (out / "reward.txt").read_text().strip() == "0.0"
    assert "missing entries" in (out / "error.txt").read_text()

    # A subset must not earn reward either.
    code, out = run(json.dumps({"trials": [_good_qa_entry("t-1")], "verdict": None}))
    assert code == 1
    assert (out / "reward.txt").read_text().strip() == "0.0"

    code, out = run(json.dumps({}))
    assert code == 1
    assert (out / "reward.txt").read_text().strip() == "0.0"

    code, out = run("not json")
    assert code == 1
    assert (out / "reward.txt").read_text().strip() == "0.0"

    code, out = run(None)
    assert code == 1
    assert (out / "reward.txt").read_text().strip() == "0.0"
    assert (out / "error.txt").read_text().strip() == (
        "the agent did not write " + str(tmp_path / "logs" / "qa_result.json")
    )


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


def test_the_validator_uses_post_trial_causality_for_good_failure():
    from oddish.worker.analysis_result_check import check_analysis_result

    must_fix_item = {
        "source": "post_trial",
        "problem_type": "incompleteness",
        "dimension": "verifier",
        "file": "tests/verify.py",
        "line_start": 4,
        "line_end": 6,
        "title": "The verifier ignores the exit code",
        "detail": "It never asserts returncode.",
        "recommendation": "Assert returncode == 0.",
        "tier": "must_fix",
        "causal": False,
    }
    expected = _qa_check_payload(["t-1"])
    entry = _good_qa_entry("t-1")
    entry["analysis"] = {
        **entry["analysis"],
        "classification": "GOOD_FAILURE",
        "action_items": [must_fix_item],
    }

    assert check_analysis_result({"trials": [entry], "verdict": None}, expected) == []

    del entry["analysis"]["action_items"][0]["causal"]
    errors = check_analysis_result({"trials": [entry], "verdict": None}, expected)
    assert any("causal is required" in error for error in errors)

    entry["analysis"]["action_items"][0]["causal"] = True
    errors = check_analysis_result({"trials": [entry], "verdict": None}, expected)
    assert any("causal post-trial must-fix" in error for error in errors)

    entry["analysis"]["classification"] = "BAD_FAILURE"
    assert check_analysis_result({"trials": [entry], "verdict": None}, expected) == []


def test_the_validator_keeps_pre_trial_must_fix_out_of_per_trial_classification():
    from oddish.worker.analysis_result_check import check_analysis_result

    expected = _qa_check_payload(
        ["t-1"],
        pre_trial_item_ids=["audit-1"],
        pre_trial_must_fix_ids=["audit-1"],
    )
    entry = _good_qa_entry("t-1")
    entry["analysis"] = {
        **entry["analysis"],
        "classification": "GOOD_FAILURE",
        "exploitation": [
            {
                "links_to": "audit-1",
                "exploited": False,
                "exploit_evidence": None,
                "causal": False,
            }
        ],
    }

    assert check_analysis_result({"trials": [entry], "verdict": None}, expected) == []


def test_the_validator_reconciles_classification_with_authoritative_trial_facts():
    """The model cannot change the trial identity, reward, or whether a
    trajectory exists. Those facts come from the database manifest."""
    from oddish.worker.analysis_result_check import check_analysis_result

    expected = _qa_check_payload(
        ["t-1"],
        trial_evidence=[
            {
                "trial_id": "t-1",
                "status": "success",
                "reward": 1.0,
                "has_trajectory": True,
                "agent": "codex",
            }
        ],
    )
    entry = _good_qa_entry("t-1")
    errors = check_analysis_result({"trials": [entry], "verdict": None}, expected)
    assert any("SUCCESS label for reward 1" in error for error in errors)
    assert any("authoritative reward 1.0" in error for error in errors)

    entry["analysis"] = {
        **entry["analysis"],
        "classification": "GOOD_SUCCESS",
        "reward": 1.0,
        "trial_name": "other-trial",
    }
    errors = check_analysis_result({"trials": [entry], "verdict": None}, expected)
    assert any("trial_name must match 't-1'" in error for error in errors)


def test_the_validator_accepts_an_empty_summary_only_without_a_trajectory():
    from oddish.worker.analysis_result_check import check_analysis_result

    expected = _qa_check_payload(
        ["t-1"],
        trial_evidence=[
            {
                "trial_id": "t-1",
                "status": "failed",
                "reward": None,
                "has_trajectory": False,
                "agent": "codex",
            }
        ],
    )
    entry = _good_qa_entry("t-1")
    entry["analysis"] = {
        **entry["analysis"],
        "classification": "HARNESS_ERROR",
        "reward": None,
    }
    entry["trajectory_summary"] = {
        "summary": "The trial recorded no trajectory.",
        "highlights": [],
        "components": [],
    }

    assert check_analysis_result({"trials": [entry], "verdict": None}, expected) == []

    entry["trajectory_summary"]["components"] = [
        {
            "step_ids": [1],
            "trajectory_component": "implementing",
            "action": "edit",
            "purpose": "build",
            "summary": "Invented work.",
        }
    ]
    errors = check_analysis_result({"trials": [entry], "verdict": None}, expected)
    assert any("components must be empty" in error for error in errors)


def test_the_validator_requires_one_exploitation_assessment_per_audit_finding():
    from oddish.worker.analysis_result_check import check_analysis_result

    expected = _qa_check_payload(["t-1"], pre_trial_item_ids=["finding-1"])
    entry = _good_qa_entry("t-1")
    errors = check_analysis_result({"trials": [entry], "verdict": None}, expected)
    assert any("missing pre-trial items" in error for error in errors)

    entry["analysis"]["exploitation"] = [
        {
            "links_to": "finding-1",
            "exploited": False,
            "exploit_evidence": None,
            "causal": False,
        }
    ]
    assert check_analysis_result({"trials": [entry], "verdict": None}, expected) == []

    entry["analysis"]["exploitation"][0]["links_to"] = {"invalid": "object"}
    errors = check_analysis_result({"trials": [entry], "verdict": None}, expected)
    assert any("links_to must be a non-empty string" in error for error in errors)


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

    # Optional fields the importer's parser still type-checks: a wrong type
    # must fail here, in the sandbox where failing buys a retry, not at
    # import where refusal is terminal.
    for key, bad in (
        ("id", 1),
        ("links_to", 7),
        ("exploited", "yes"),
        ("causal", "no"),
        ("exploit_evidence", 3),
    ):
        typed = dict(item)
        typed[key] = bad
        assert check_analysis_result({"items": [typed]}, expected), key
    optional_ok = dict(
        item,
        id=None,
        links_to="a1",
        exploited=False,
        causal=False,
        exploit_evidence=None,
    )
    assert check_analysis_result({"items": [optional_ok]}, expected) == []

    invalid_range = dict(item, line_start=7, line_end=6)
    errors = check_analysis_result({"items": [invalid_range]}, expected)
    assert any("greater than or equal" in error for error in errors)


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


def _qa_run_trajectory(graded_ids: list[str]) -> dict:
    """A stereotyped QA run: plan, fetch each trial, read the data, inspect,
    write the artifact. Step 4 mixes a fetch with a read so precedence is
    exercised, and the fetch commands name the graded trials so the mention
    scan has something to find."""
    return {
        "agent": "claude-code",
        "steps": [
            {"step_id": 1, "timestamp": "2026-08-18T00:00:00Z", "tool_calls": []},
            {
                "step_id": 2,
                "timestamp": "2026-08-18T00:00:05Z",
                "tool_calls": [
                    {
                        "name": "Bash",
                        "arguments": {
                            "command": f"node /probe-harness/oddish-query trajectory {graded_ids[0]}"
                        },
                    }
                ],
            },
            {
                "step_id": 3,
                "timestamp": "2026-08-18T00:00:10Z",
                "tool_calls": [
                    {
                        "name": "Read",
                        "arguments": {"file_path": f"/tmp/data/{graded_ids[1]}.json"},
                    }
                ],
            },
            {
                "step_id": 4,
                "timestamp": "2026-08-18T00:00:15Z",
                "tool_calls": [
                    {"name": "Read", "arguments": {"file_path": "/tmp/notes.md"}},
                    {
                        "name": "Bash",
                        "arguments": {
                            "command": f"node /probe-harness/oddish-query logs {graded_ids[1]}"
                        },
                    },
                ],
            },
            {
                "step_id": 5,
                "timestamp": "2026-08-18T00:00:20Z",
                "tool_calls": [
                    {"name": "Bash", "arguments": {"command": "jq .steps /tmp/t.json"}}
                ],
            },
            {
                "step_id": 6,
                "timestamp": "2026-08-18T00:00:25Z",
                "tool_calls": [
                    {
                        "name": "Write",
                        "arguments": {"file_path": "/logs/qa_result.json"},
                    }
                ],
            },
        ],
    }


@pytest.mark.asyncio
async def test_a_settled_qa_trial_summarizes_its_own_run(monkeypatch):
    """Needs a database. Settlement writes the QA trial's own deterministic
    trajectory_summary (independent of the artifact import) and stamps
    ``_graded_at_steps`` anchors onto each graded trial."""
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from oddish.analyze.analysis_activity import ANALYSIS_ACTIVITY_VERSION
    from oddish.db import TaskStatus, TrialStatus, get_session, init_db
    from oddish.db.models import ExperimentModel, TaskModel
    from oddish.workers import analysis_trials
    from oddish.workers.analysis_trials import handle_analysis_trial_settled

    await init_db()
    run = uuid.uuid4().hex[:8]
    task_id = f"qa-selfsum-{run}"
    graded_ids = [f"{task_id}-graded-{i}-{uuid.uuid4().hex}" for i in (1, 2)]
    qa_id = f"{task_id}-qa"
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
                model="anthropic/claude-opus-5",
                status=TrialStatus.SUCCESS,
                # The settlement gate: without it the self-summary skips the
                # trajectory read entirely.
                has_trajectory=True,
                attempts=1,
                max_attempts=3,
                harbor_config={
                    "analysis_payload": {
                        "trial_ids": graded_ids,
                        "with_verdict": False,
                    },
                },
            )
        )
        await session.commit()

    qa_trajectory = _qa_run_trajectory(graded_ids)

    async def fake_trajectory(row):
        # Only the QA run has a trajectory; the graded rows read as absent so
        # their summaries carry version stamps but no derived facts.
        return qa_trajectory if row.id == qa_id else None

    monkeypatch.setattr("oddish.core.trial_io.read_trial_trajectory", fake_trajectory)
    artifact = {
        "trials": [_good_qa_entry(t) for t in graded_ids],
        "verdict": None,
    }

    async def read_artifact(trial, filename):
        return artifact

    monkeypatch.setattr(analysis_trials, "read_analysis_artifact", read_artifact)

    await handle_analysis_trial_settled(qa_id)

    async with get_session() as session:
        qa_row = await session.get(TrialModel, qa_id)
        own = qa_row.trajectory_summary
        assert own is not None
        assert own["generator"] == "analysis-activity"
        assert own["taxonomy_version"] == ANALYSIS_ACTIVITY_VERSION
        assert own["model"] == "anthropic/claude-opus-5"
        # Enrichment ran over the counted components: derived facts present.
        assert own["components"][1]["trajectory_component"] == "fetching_trial_data"
        assert own["components"][1]["tool_count"] == 1
        assert own["components"][1]["duration_ms"] == 5000

        first, second = [
            await session.get(TrialModel, trial_id) for trial_id in graded_ids
        ]
        assert first.analysis["_graded_at_steps"] == [2]
        assert second.analysis["_graded_at_steps"] == [3, 4]
        task = await session.get(TaskModel, task_id)
        assert task.status == TaskStatus.COMPLETED


def test_the_summarize_brief_names_its_output_and_target():
    from oddish.workers.analysis_trials import build_summarize_brief

    brief = build_summarize_brief(task_name="apache-kafka", target_trial_id="t-42")
    assert "/logs/summary_result.json" in brief
    assert '"target_trial_id": "t-42"' in brief
    assert "reading_files" in brief and "debugging" in brief
    assert "Do not solve the task" in brief
    assert "oddish-query" not in brief


def test_the_materialized_summarize_brief_contains_bounded_trial_data():
    from oddish.workers.analysis_trials import build_summarize_brief

    brief = build_summarize_brief(
        task_name="apache-kafka",
        target_trial_id="t-42",
        trajectory={
            "steps": [{"step_id": 1, "source": "agent", "message": "finished"}]
        },
        instruction="repair the broker",
        final_reward=1.0,
        model_used="anthropic/claude-test",
        verifier_output="all tests passed",
    )

    assert "repair the broker" in brief
    assert "Final reward: 1.0" in brief
    assert '"step_id":1' in brief
    assert "all tests passed" in brief


@pytest.mark.asyncio
async def test_materialize_summarize_brief_reads_the_target_without_the_api(
    monkeypatch,
):
    from types import SimpleNamespace

    from oddish.core import trial_io
    from oddish.db import TaskModel
    from oddish.workers import analysis_trials

    target = SimpleNamespace(
        id="t-42",
        task_id="task-1",
        kind="agent",
        has_trajectory=True,
        reward=1.0,
        model="anthropic/claude-test",
    )
    task = SimpleNamespace(id="task-1", name="apache-kafka")

    class Session:
        async def get(self, model, row_id):
            if model is TrialModel and row_id == "t-42":
                return target
            if model is TaskModel and row_id == "task-1":
                return task
            return None

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_args):
            return None

    async def read_summary_inputs(_target):
        return (
            {"steps": [{"step_id": 1, "source": "agent", "message": "done"}]},
            "repair the broker",
            "all tests passed",
        )

    monkeypatch.setattr(analysis_trials, "get_session", SessionContext)
    monkeypatch.setattr(trial_io, "read_trial_summary_inputs", read_summary_inputs)

    brief = await analysis_trials.materialize_summarize_brief(
        {"analysis_payload": {"target_trial_id": "t-42"}}
    )

    assert "repair the broker" in brief
    assert '"step_id":1' in brief
    assert "oddish-query" not in brief


def test_the_validator_enforces_the_summarize_contract():
    from oddish.worker.analysis_result_check import check_analysis_result

    expected = {"kind": "summarize", "target_trial_id": "t-42"}
    good = {
        "target_trial_id": "t-42",
        "trajectory_summary": _good_qa_entry("t-42")["trajectory_summary"],
    }
    assert check_analysis_result(good, expected) == []
    wrong_target = {**good, "target_trial_id": "t-9"}
    assert any(
        "target_trial_id" in violation
        for violation in check_analysis_result(wrong_target, expected)
    )
    assert check_analysis_result(
        {"target_trial_id": "t-42", "trajectory_summary": {}}, expected
    )


@pytest.mark.asyncio
async def test_analysis_artifact_storage_errors_remain_retryable(monkeypatch):
    """A storage outage must reach the cleanup retry path, not look absent."""
    from types import SimpleNamespace

    from oddish.workers import analysis_trials

    class UnavailableStorage:
        async def object_exists(self, _key):
            raise TimeoutError("storage timed out")

    monkeypatch.setattr(
        analysis_trials, "get_storage_client", lambda: UnavailableStorage()
    )

    with pytest.raises(TimeoutError, match="storage timed out"):
        await analysis_trials.read_analysis_artifact(
            SimpleNamespace(id="task-1-9", trial_s3_key="trials/task-1-9/"),
            "summary_result.json",
        )


async def _seed_summarize_targets(
    prefix: str, specs: list[tuple[str, str, bool]]
) -> tuple[str, dict[str, str]]:
    """Create one task and its candidate summarize targets for DB tests."""
    from sqlalchemy import text

    from oddish.db import TaskStatus, TrialStatus, get_session
    from oddish.db.models import ExperimentModel, TaskModel

    run = uuid.uuid4().hex[:8]
    task_id = f"{prefix}-{run}"
    ids = {label: f"{task_id}-{label}-{uuid.uuid4().hex}" for label, _, _ in specs}
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
            )
        )
        await session.flush()
        await session.execute(
            text(
                "INSERT INTO task_experiments (task_id, experiment_id, created_at) "
                "VALUES (:task_id, :experiment_id, NOW())"
            ),
            {"task_id": task_id, "experiment_id": experiment.id},
        )
        for label, kind, has_trajectory in specs:
            session.add(
                TrialModel(
                    id=ids[label],
                    name=ids[label],
                    task_id=task_id,
                    experiment_id=experiment.id,
                    agent="claude-code",
                    provider="local",
                    queue_key="q",
                    kind=kind,
                    status=TrialStatus.SUCCESS,
                    has_trajectory=has_trajectory,
                    attempts=1,
                    max_attempts=3,
                )
            )
    return task_id, ids


@pytest.mark.asyncio
async def test_summarize_creation_accepts_only_agent_targets_and_imports_only_summary(
    monkeypatch,
):
    """Needs a database. Paid generation accepts an agent trajectory only;
    settlement changes that target's summary and no analysis or non-agent row."""
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from oddish.db import TrialStatus, get_session, init_db
    from oddish.workers import analysis_trials
    from oddish.workers.analysis_trials import (
        get_or_create_summarize_trial,
        handle_analysis_trial_settled,
    )

    await init_db()
    task_id, ids = await _seed_summarize_targets(
        "summarize-targets",
        [
            ("agent", "agent", True),
            ("bare", "agent", False),
            ("qa", "qa", True),
            ("audit", "audit", True),
            ("summarize", "summarize", True),
        ],
    )
    async with get_session() as session:
        for label in ("bare", "qa", "audit", "summarize"):
            assert (
                await get_or_create_summarize_trial(session, target_trial_id=ids[label])
                is None
            )
        created = await get_or_create_summarize_trial(
            session, target_trial_id=ids["agent"]
        )
        assert created is not None
        summarize_id = created.id
        target = await session.get(TrialModel, ids["agent"])
        assert target.trajectory_summary_refresh_trial_id == summarize_id

    async with get_session() as session:
        target = await session.get(TrialModel, ids["agent"])
        target.analysis = {"sentinel": True}
        row = await session.get(TrialModel, summarize_id)
        assert row.agent == "single-llm"
        assert row.harbor_config["agent_config"]["import_path"].endswith(
            ":SingleLLMAgent"
        )
        row.status = TrialStatus.SUCCESS

    artifact = {
        "target_trial_id": ids["agent"],
        "trajectory_summary": _good_qa_entry(ids["agent"])["trajectory_summary"],
    }

    async def read_artifact(trial, filename):
        assert filename == "summary_result.json"
        return artifact

    async def no_trajectory(row):
        return None

    monkeypatch.setattr(analysis_trials, "read_analysis_artifact", read_artifact)
    monkeypatch.setattr("oddish.core.trial_io.read_trial_trajectory", no_trajectory)
    await handle_analysis_trial_settled(summarize_id)

    async with get_session() as session:
        target = await session.get(TrialModel, ids["agent"])
        assert target.task_id == task_id
        assert target.trajectory_summary["_graded_by"] == summarize_id
        assert target.trajectory_summary_refresh_trial_id is None
        assert target.analysis == {"sentinel": True}
        for label in ("qa", "audit", "summarize"):
            row = await session.get(TrialModel, ids[label])
            assert row.trajectory_summary is None


@pytest.mark.asyncio
async def test_paused_summarize_trial_is_adopted_instead_of_replaced():
    """Needs PostgreSQL. A paused refresh still owns its worker and target."""
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from oddish.db import TrialStatus, get_session, init_db
    from oddish.workers.analysis_trials import get_or_create_summarize_trial

    await init_db()
    _, ids = await _seed_summarize_targets(
        "summarize-paused", [("agent", "agent", True)]
    )
    target_id = ids["agent"]
    async with get_session() as session:
        created = await get_or_create_summarize_trial(
            session, target_trial_id=target_id
        )
        assert created is not None
        summarize_id = created.id

    async with get_session() as session:
        summarize = await session.get(TrialModel, summarize_id)
        summarize.status = TrialStatus.PAUSED

    async with get_session() as session:
        adopted = await get_or_create_summarize_trial(
            session, target_trial_id=target_id
        )
        assert adopted is not None
        assert adopted.id == summarize_id


@pytest.mark.asyncio
async def test_missing_summarize_artifact_fails_refresh_and_allows_replacement(
    monkeypatch,
):
    """Needs PostgreSQL. A permanently absent artifact must not stay settling."""
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from oddish.db import TrialStatus, get_session, init_db
    from oddish.workers import analysis_trials
    from oddish.workers.analysis_trials import (
        get_or_create_summarize_trial,
        handle_analysis_trial_settled,
    )

    await init_db()
    _, ids = await _seed_summarize_targets(
        "summarize-missing-artifact", [("agent", "agent", True)]
    )
    target_id = ids["agent"]
    async with get_session() as session:
        summarize = await get_or_create_summarize_trial(
            session, target_trial_id=target_id
        )
        assert summarize is not None
        summarize_id = summarize.id
    async with get_session() as session:
        summarize = await session.get(TrialModel, summarize_id)
        summarize.status = TrialStatus.SUCCESS
        summarize.reward = 1.0

    async def missing_artifact(_trial, _filename):
        return None

    monkeypatch.setattr(analysis_trials, "read_analysis_artifact", missing_artifact)
    await handle_analysis_trial_settled(summarize_id)

    async with get_session() as session:
        target = await session.get(TrialModel, target_id)
        failed = await session.get(TrialModel, summarize_id)
        assert target.trajectory_summary_refresh_trial_id == summarize_id
        assert failed.status == TrialStatus.FAILED
        assert failed.reward is None
        assert failed.error_message == (
            "Trajectory summary import failed: produced no valid summary_result.json"
        )

    async with get_session() as session:
        replacement = await get_or_create_summarize_trial(
            session, target_trial_id=target_id
        )
        assert replacement is not None
        assert replacement.id != summarize_id
        target = await session.get(TrialModel, target_id)
        assert target.trajectory_summary_refresh_trial_id == replacement.id


@pytest.mark.asyncio
async def test_concurrent_summarize_creation_returns_one_trial_and_one_worker_job():
    """Needs PostgreSQL. The target-row lock serializes two paid refreshes."""
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    import asyncio

    from sqlalchemy import select

    from oddish.db import get_session, init_db
    from oddish.db.models import WorkerJobKind, WorkerJobModel
    from oddish.workers.analysis_trials import get_or_create_summarize_trial

    await init_db()
    _, ids = await _seed_summarize_targets("summarize-race", [("agent", "agent", True)])

    async def request_summary() -> str:
        async with get_session() as session:
            trial = await get_or_create_summarize_trial(
                session, target_trial_id=ids["agent"]
            )
            assert trial is not None
            return trial.id

    first_id, second_id = await asyncio.gather(request_summary(), request_summary())
    assert first_id == second_id

    async with get_session() as session:
        summarize_trials = (
            await session.scalars(
                select(TrialModel).where(
                    TrialModel.kind == "summarize",
                    TrialModel.harbor_config["analysis_payload"][
                        "target_trial_id"
                    ].astext
                    == ids["agent"],
                )
            )
        ).all()
        jobs = (
            await session.scalars(
                select(WorkerJobModel).where(
                    WorkerJobModel.kind == WorkerJobKind.TRIAL,
                    WorkerJobModel.subject_id == first_id,
                )
            )
        ).all()
        assert [trial.id for trial in summarize_trials] == [first_id]
        assert len(jobs) == 1


@pytest.mark.asyncio
async def test_concurrent_summarize_creation_for_two_targets_reserves_unique_ids():
    """Needs PostgreSQL. The task lock serializes ids across target rows."""
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    import asyncio

    from oddish.db import get_session, init_db
    from oddish.workers.analysis_trials import get_or_create_summarize_trial

    await init_db()
    _, ids = await _seed_summarize_targets(
        "summarize-two-targets",
        [("first", "agent", True), ("second", "agent", True)],
    )

    async def request_summary(target_id: str) -> str:
        async with get_session() as session:
            trial = await get_or_create_summarize_trial(
                session, target_trial_id=target_id
            )
            assert trial is not None
            return trial.id

    first_id, second_id = await asyncio.gather(
        request_summary(ids["first"]), request_summary(ids["second"])
    )
    assert first_id != second_id

    async with get_session() as session:
        first = await session.get(TrialModel, ids["first"])
        second = await session.get(TrialModel, ids["second"])
        assert first.trajectory_summary_refresh_trial_id == first_id
        assert second.trajectory_summary_refresh_trial_id == second_id


@pytest.mark.asyncio
async def test_older_summarize_import_cannot_overwrite_newer_refresh(monkeypatch):
    """Needs PostgreSQL. Publication compares the target pointer under lock."""
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from oddish.db import TrialStatus, get_session, init_db
    from oddish.workers import analysis_trials
    from oddish.workers.analysis_trials import (
        get_or_create_summarize_trial,
        handle_analysis_trial_settled,
    )

    await init_db()
    _, ids = await _seed_summarize_targets(
        "summarize-stale-import", [("agent", "agent", True)]
    )
    target_id = ids["agent"]
    async with get_session() as session:
        older = await get_or_create_summarize_trial(session, target_trial_id=target_id)
        assert older is not None
        older_id = older.id
    async with get_session() as session:
        older = await session.get(TrialModel, older_id)
        older.status = TrialStatus.FAILED
    async with get_session() as session:
        newer = await get_or_create_summarize_trial(session, target_trial_id=target_id)
        assert newer is not None
        newer_id = newer.id
        assert newer_id != older_id
    async with get_session() as session:
        target = await session.get(TrialModel, target_id)
        target.trajectory_summary = {"sentinel": "newer publication pending"}
        older = await session.get(TrialModel, older_id)
        older.status = TrialStatus.SUCCESS

    async def read_artifact(_trial, _filename):
        return {
            "target_trial_id": target_id,
            "trajectory_summary": _good_qa_entry(target_id)["trajectory_summary"],
        }

    monkeypatch.setattr(analysis_trials, "read_analysis_artifact", read_artifact)
    await handle_analysis_trial_settled(older_id)

    async with get_session() as session:
        target = await session.get(TrialModel, target_id)
        assert target.trajectory_summary == {"sentinel": "newer publication pending"}
        assert target.trajectory_summary_refresh_trial_id == newer_id


@pytest.mark.asyncio
async def test_summarize_worker_job_is_reported_as_analysis_not_agent_work():
    """Needs PostgreSQL. A summarize sandbox uses a TRIAL worker job, but
    queue diagnostics must keep it out of ordinary agent-trial totals."""
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from sqlalchemy import select

    from oddish.core.admin import get_queue_status_core
    from oddish.db import get_session, init_db
    from oddish.db.models import WorkerJobKind, WorkerJobModel
    from oddish.workers.analysis_trials import get_or_create_summarize_trial

    await init_db()
    _, ids = await _seed_summarize_targets(
        "summarize-queue-kind", [("agent", "agent", True)]
    )
    queue_key = f"summarize-test-{uuid.uuid4().hex}"

    async with get_session() as session:
        analysis_queued_before = (await get_queue_status_core(session)).analysis_queued
        summarize = await get_or_create_summarize_trial(
            session, target_trial_id=ids["agent"]
        )
        assert summarize is not None
        job = await session.scalar(
            select(WorkerJobModel).where(
                WorkerJobModel.kind == WorkerJobKind.TRIAL,
                WorkerJobModel.subject_id == summarize.id,
            )
        )
        assert job is not None
        summarize.queue_key = queue_key
        job.queue_key = queue_key
        await session.flush()

        status = await get_queue_status_core(session)

    entries = [entry for entry in status.queues if entry.queue_key == queue_key]
    assert [(entry.kind, entry.queued, entry.running) for entry in entries] == [
        ("SUMMARIZE", 1, 0)
    ]
    assert all(entry.queue_key != queue_key for entry in status.trial_queues)
    assert status.analysis_queued == analysis_queued_before + 1


@pytest.mark.asyncio
async def test_reimport_scan_miss_keeps_same_grader_step_anchors(monkeypatch):
    """Needs a database. A healer re-import whose grader-trajectory read
    failed (``own_trajectory=None``, the best-effort read's failure value)
    must keep the ``_graded_at_steps`` anchors the first import stored — and
    a *different* grader's scan miss must not inherit them, because anchors
    index into the grader's own trajectory."""
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from oddish.db import TaskStatus, TrialStatus, get_session, init_db
    from oddish.db.models import ExperimentModel, TaskModel
    from oddish.workers import analysis_trials
    from oddish.workers.analysis_trials import handle_analysis_trial_settled

    await init_db()
    run = uuid.uuid4().hex[:8]
    task_id = f"qa-anchor-keep-{run}"
    graded_ids = [f"{task_id}-graded-{i}-{uuid.uuid4().hex}" for i in (1, 2)]
    qa_id = f"{task_id}-qa"
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
                model="anthropic/claude-opus-5",
                status=TrialStatus.SUCCESS,
                has_trajectory=True,
                attempts=1,
                max_attempts=3,
                harbor_config={
                    "analysis_payload": {
                        "trial_ids": graded_ids,
                        "with_verdict": False,
                    },
                },
            )
        )
        await session.commit()

    qa_trajectory = _qa_run_trajectory(graded_ids)

    async def fake_trajectory(row):
        return qa_trajectory if row.id == qa_id else None

    monkeypatch.setattr("oddish.core.trial_io.read_trial_trajectory", fake_trajectory)
    artifact = {
        "trials": [_good_qa_entry(t) for t in graded_ids],
        "verdict": None,
    }

    async def read_artifact(trial, filename):
        return artifact

    monkeypatch.setattr(analysis_trials, "read_analysis_artifact", read_artifact)

    await handle_analysis_trial_settled(qa_id)

    async with get_session() as session:
        first = await session.get(TrialModel, graded_ids[0])
        assert first.analysis["_graded_at_steps"] == [2]
        qa_row = await session.get(TrialModel, qa_id)

    # Same grader re-imports with the trajectory read failing: the scan
    # misses, the stored anchors survive.
    await analysis_trials._import_qa_result(qa_row, own_trajectory=None)
    async with get_session() as session:
        first = await session.get(TrialModel, graded_ids[0])
        assert first.analysis["_graded_by"] == qa_id
        assert first.analysis["_graded_at_steps"] == [2]

    # A different grader's scan miss must not inherit another run's anchors.
    async with get_session() as session:
        row = await session.get(TrialModel, graded_ids[0])
        row.analysis = {**row.analysis, "_graded_by": "some-other-qa-trial"}
    await analysis_trials._import_qa_result(qa_row, own_trajectory=None)
    async with get_session() as session:
        first = await session.get(TrialModel, graded_ids[0])
        assert first.analysis["_graded_by"] == qa_id
        assert "_graded_at_steps" not in first.analysis


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


def test_antigravity_provenance_reads_agys_pascalcase_path_argument():
    """agy records its write path as ``TargetFile``, and its own tool names.

    Harbor's ATIF writer copies agy's tool name AND its argument dict through
    unchanged, so both spellings below are what a real trajectory holds --
    the shapes here are taken from a recorded agy 1.1.19 run. Before
    ``TargetFile`` was a known path key, every agy write resolved to no path:
    the agent was reported provenance-CAPABLE while never attributing a single
    file, which reads as "it did not revisit its own work" rather than "we
    cannot see". ``edit_file`` and ``multi_replace_file_content`` cover the
    other half -- a write tool absent from the map is a revisit that never
    counts. Each of the three writes a DISTINCT path, so every name has to be
    recognized for the final set to be complete.
    """
    from oddish.analyze.trajectory_provenance import authored_paths_by_step

    def write(step_id: int, name: str, path: str) -> dict:
        return {
            "step_id": step_id,
            "tool_calls": [{"function_name": name, "arguments": {"TargetFile": path}}],
        }

    trajectory = {
        "agent": "antigravity-cli",
        "steps": [
            {
                "step_id": 1,
                "tool_calls": [
                    {
                        "function_name": "write_to_file",
                        "arguments": {
                            "CodeContent": "Hello, world!\n",
                            "Overwrite": True,
                            "TargetFile": "/app/hello.txt",
                        },
                    }
                ],
            },
            write(2, "edit_file", "/app/edited.py"),
            write(3, "multi_replace_file_content", "/app/multi.py"),
            {"step_id": 4, "tool_calls": []},
        ],
    }

    prior = authored_paths_by_step(trajectory)
    # Strictly "before": the step that creates a file is authoring it.
    assert prior[1] == set()
    assert prior[2] == {"/app/hello.txt"}
    assert prior[3] == {"/app/hello.txt", "/app/edited.py"}
    assert prior[4] == {"/app/hello.txt", "/app/edited.py", "/app/multi.py"}


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

    for trial_kind in ("qa", "qa_eval", "audit", "summarize"):
        assert should_generate_inline_probe_summary(trial_kind, "the brief") is False
    assert should_generate_inline_probe_summary("agent", "probe instructions") is True
    assert should_generate_inline_probe_summary("agent", None) is False
    assert should_generate_inline_probe_summary("agent", "") is False


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


@pytest.mark.parametrize("verdict", [None, "accept", "reject"])
@pytest.mark.parametrize("defect", ["oracle_copying", None])
@pytest.mark.asyncio
async def test_qa_import_replaces_old_acceptance_with_only_the_current_verdict(
    monkeypatch, verdict, defect
):
    """The base64 rerun: 14 runs, two agents, an old accept, and three leaks.

    Run the real importer, verdict writer, and state transitions. Only storage
    and the database connection are replaced with in-memory records.
    """
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from oddish.core import verdict_sync
    from oddish.db import (
        TaskModel,
        TaskVersionModel,
        TaskStatus,
        TrialStatus,
        VerdictStatus,
    )
    from oddish.workers import analysis_trials

    with_verdict = verdict is not None
    task = SimpleNamespace(
        id="base64-task",
        current_version_id="base64-task-v3",
        status=TaskStatus.VERDICT_PENDING,
        verdict={"is_good": True, "task_problem_count": 0},
        verdict_status=VerdictStatus.QUEUED,
        verdict_error=None,
        verdict_started_at=None,
        verdict_finished_at=None,
    )
    version = SimpleNamespace(
        pre_trial={"items": []}, pre_trial_status=VerdictStatus.SUCCESS
    )
    sources = {
        f"trial-{i}": SimpleNamespace(
            id=f"trial-{i}", task_id=task.id, reward=1.0, analysis=None
        )
        for i in range(14)
    }
    entries = []
    for i, trial_id in enumerate(sources):
        entry = _good_qa_entry(trial_id)
        entry["analysis"].update(
            classification="GOOD_SUCCESS", subtype="legitimate_solution", reward=1.0
        )
        if i < 3 and defect == "oracle_copying":
            # Trial findings must be stored independently of the overall verdict.
            entry["analysis"].update(
                classification="BAD_SUCCESS",
                subtype="oracle_copying",
                evidence="The agent copied the protected oracle (trajectory step 30).",
                recommendation="Prevent the agent from reading the oracle bytes.",
            )
        entries.append(entry)
    candidate = (
        TaskVerdictModel(verdict=verdict, confidence="high").model_dump()
        if with_verdict
        else None
    )
    artifact = {"trials": entries, "verdict": candidate if with_verdict else None}
    qa = SimpleNamespace(
        id="qa-new",
        superseded_by_trial_id=None,
        harbor_stage=None,
        task_id=task.id,
        task_version_id=task.current_version_id,
        status=TrialStatus.SUCCESS,
        model="qa-model",
        harbor_config={
            "analysis_payload": {
                "trial_ids": list(sources),
                "with_verdict": with_verdict,
                "trial_evidence": [
                    {
                        "trial_id": trial_id,
                        "status": "success",
                        "reward": 1.0,
                        "has_trajectory": True,
                        "agent": "grok-build" if i < 8 else "mini-swe-agent",
                    }
                    for i, trial_id in enumerate(sources)
                ],
            }
        },
    )

    class Session:
        async def get(self, model, row_id, **kwargs):
            if model is TaskModel:
                assert row_id == task.id
                return task
            if model is TaskVersionModel:
                return version
            assert model is TrialModel
            return qa if row_id == qa.id else sources[row_id]

        async def scalar(self, statement):
            # The import's current-version and all-trials-settled guards.
            if "tasks.current_version_id" in str(statement):
                return task.current_version_id
            if "ORDER BY" in str(statement):
                return qa.id
            return None

        async def commit(self):
            pass

    @asynccontextmanager
    async def session():
        yield Session()

    monkeypatch.setattr(analysis_trials, "get_session", session)
    monkeypatch.setattr(verdict_sync, "get_session", session)
    monkeypatch.setattr(
        analysis_trials, "read_analysis_artifact", AsyncMock(return_value=artifact)
    )
    monkeypatch.setattr(
        analysis_trials, "aggregate_exploited_into_pre_trial", AsyncMock()
    )
    monkeypatch.setattr(
        "oddish.core.trial_io.read_trial_trajectory", AsyncMock(return_value=None)
    )

    await analysis_trials._import_qa_result(qa)

    assert task.status == TaskStatus.COMPLETED
    assert task.verdict_status == VerdictStatus.SUCCESS
    assert task.verdict_error is None
    assert all(row.analysis["_graded_by"] == qa.id for row in sources.values())
    assert sum(
        row.analysis["classification"] == "BAD_SUCCESS" for row in sources.values()
    ) == (3 if defect else 0)
    if with_verdict:
        assert task.verdict["verdict"] == verdict
        assert task.verdict["is_good"] is (verdict == "accept")
        assert task.verdict["_graded_by"] == qa.id
    else:
        assert task.verdict is None
