"""_store must persist the per-trial findings it currently discards."""

from dataclasses import asdict

from oddish.evals.analyzer.schemas import Finding


def _finding(trial_id="t1", model="claude-opus-4-8") -> Finding:
    return Finding(
        trial_id=trial_id, bucket="bad", subcategory="1b",
        evidence_quote="hardcoded the expected output", step_ids=[3, 7],
        root_cause="test asserts a literal", headroom_signal="",
        trajectory_link=f"/tasks/task/probe/{trial_id}", model=model,
    )


def test_finding_is_json_native():
    """Finding must survive asdict -> JSONB -> read back with no custom encoder."""
    import json

    d = asdict(_finding())
    assert json.loads(json.dumps(d)) == d
    assert d["model"] == "claude-opus-4-8"
    assert d["step_ids"] == [3, 7]


def test_analyzer_model_has_a_findings_column():
    from oddish.db.models import AnalyzerModel

    col = AnalyzerModel.__table__.columns["findings"]
    assert col.nullable is True


def test_analyzer_model_has_a_models_by_task_column():
    from oddish.db.models import AnalyzerModel

    col = AnalyzerModel.__table__.columns["models_by_task"]
    assert col.nullable is True
