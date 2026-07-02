from oddish.cli.api import build_sweep_payload
from oddish.core.idempotency import compute_request_hash
from oddish.schemas import AgentModelPair, TaskSweepSubmission


def _payload(**kwargs):
    return build_sweep_payload(
        task_id="task",
        configs=[{"agent": "nop", "n_trials": 1}],
        environment=None,
        user=None,
        priority="low",
        experiment_id=None,
        **kwargs,
    )


def test_build_sweep_payload_forwards_github_id():
    payload = _payload(github_id="123456")
    assert payload["github_id"] == "123456"


def test_build_sweep_payload_omits_github_id_when_absent():
    assert "github_id" not in _payload()


def test_payload_round_trips_into_submission_with_github_id():
    payload = build_sweep_payload(
        task_id="task_lg",
        configs=[
            {"agent": "claude-code", "model": "anthropic/claude-sonnet-4-6", "n_trials": 1}
        ],
        environment=None,
        user=None,
        priority="low",
        experiment_id=None,
        github_username="octocat",
        github_id="123456",
    )
    submission = TaskSweepSubmission.model_validate(
        {**payload, "configs": [AgentModelPair(**c) for c in payload["configs"]]}
    )
    assert submission.github_id == "123456"
    assert submission.github_username == "octocat"


def _submission(**kwargs) -> TaskSweepSubmission:
    return TaskSweepSubmission(
        task_id="task_lg",
        configs=[AgentModelPair(agent="nop", model="nop/nop", n_trials=1)],
        user=None,
        **kwargs,
    )


def test_unset_github_id_does_not_change_request_hash():
    """A submission that omits github_id must fingerprint identically to its
    pre-github_id form, so an in-flight Idempotency-Key retried across the
    deploy boundary does not spuriously 409."""
    data = _submission().model_dump(mode="json")
    data.pop("github_id")  # the body a pre-github_id client/server would hash
    legacy_hash = compute_request_hash(_submission())  # github_id is None here
    from oddish.core.idempotency import (
        _canonical_digest,
        _registry_auth_fingerprints,
    )

    # compute_request_hash also fingerprints registry_auth; mirror that on the
    # hand-built body so this test isolates the github_id-drop behavior.
    if "registry_auth" in data:
        data["registry_auth"] = _registry_auth_fingerprints(data.get("registry_auth"))
    assert legacy_hash == _canonical_digest(data)


def test_set_github_id_changes_request_hash():
    """A key reused with a different github_id must still be a body conflict."""
    assert compute_request_hash(_submission()) != compute_request_hash(
        _submission(github_id="123456")
    )
