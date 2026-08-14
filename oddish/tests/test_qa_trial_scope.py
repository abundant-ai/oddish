from sqlalchemy.dialects import postgresql

from oddish.core.qa_scope import (
    analysis_fingerprint,
    input_set_sha256,
    live_same_version_trial_scope,
    qa_classification_scope,
)


def _sql(expression) -> str:
    return str(
        expression.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_live_and_classification_scopes_share_version_and_terminal_rules() -> None:
    live = _sql(live_same_version_trial_scope("task-1", "task-1-v2"))
    classified = _sql(qa_classification_scope("task-1", "task-1-v2"))

    for fragment in (
        "trials.task_id = 'task-1'",
        "trials.task_version_id = 'task-1-v2'",
        "trials.superseded_by_trial_id IS NULL",
        "trials.harbor_stage",
        "trials.status != 'SKIPPED'",
        "Skipped by baseline gate",
    ):
        assert fragment in live
        assert fragment in classified

    assert "trials.imported_at IS NULL" not in live
    assert "trials.imported_at IS NULL" in classified
    assert "trials.is_probe IS false" in classified
    assert "lower" in classified  # canonical baseline-agent predicate


def test_analysis_and_input_set_fingerprints_are_canonical() -> None:
    first = {"b": [2, 1], "a": {"x": True}}
    reordered = {"a": {"x": True}, "b": [2, 1]}
    assert analysis_fingerprint(first) == analysis_fingerprint(reordered)
    assert analysis_fingerprint(first).startswith("sha256:")

    fingerprints = {"trial-b": "sha256:b", "trial-a": "sha256:a"}
    assert input_set_sha256(fingerprints) == input_set_sha256(
        dict(reversed(list(fingerprints.items())))
    )
