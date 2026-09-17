import httpx

from oddish.cli.api import _extract_error_detail, build_sweep_payload
from oddish.core.idempotency import compute_request_hash
from oddish.schemas import AgentModelPair, TaskSweepSubmission


def test_extract_error_detail_surfaces_403_gate_message():
    resp = httpx.Response(
        403,
        json={
            "detail": (
                "GitHub account 42 is not connected to an oddish user in this org."
            )
        },
    )
    assert _extract_error_detail(resp) == (
        "GitHub account 42 is not connected to an oddish user in this org."
    )


def test_extract_error_detail_falls_back_to_body_without_string_detail():
    assert _extract_error_detail(httpx.Response(500, text="boom")) == "boom"
    assert (
        _extract_error_detail(httpx.Response(422, json={"detail": [{"msg": "x"}]}))
        == '{"detail":[{"msg":"x"}]}'
    )


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
    from oddish.core.idempotency import _canonical_digest, _registry_auth_fingerprints

    submission = _submission()  # github_id is None
    # The body a pre-github_id client/server would hash: the same pipeline
    # compute_request_hash runs, minus the github_id key.
    data = submission.model_dump(mode="json")
    for absent_in_old_bodies in ("github_id", "add_trials", "requires_gpu", "gpu_types"):
        data.pop(absent_in_old_bodies, None)
    if hasattr(submission, "registry_auth"):
        data["registry_auth"] = _registry_auth_fingerprints(
            getattr(submission, "registry_auth", None)
        )
    assert compute_request_hash(submission) == _canonical_digest(data)


def test_set_github_id_changes_request_hash():
    """A key reused with a different github_id must still be a body conflict."""
    assert compute_request_hash(_submission()) != compute_request_hash(
        _submission(github_id="123456")
    )


def test_unset_requires_gpu_keeps_the_pre_field_request_hash():
    """Clients that predate ``requires_gpu`` never send it; an in-flight
    Idempotency-Key retried across the deploy boundary must not 409."""
    from oddish.core.idempotency import _canonical_digest, _registry_auth_fingerprints

    submission = _submission()  # requires_gpu defaults to False
    data = submission.model_dump(mode="json")
    for absent_in_old_bodies in ("requires_gpu", "gpu_types", "github_id", "add_trials"):
        data.pop(absent_in_old_bodies, None)
    data["registry_auth"] = _registry_auth_fingerprints(None)
    assert compute_request_hash(submission) == _canonical_digest(data)
    assert compute_request_hash(_submission(requires_gpu=True)) != compute_request_hash(
        submission
    )
    assert compute_request_hash(
        _submission(requires_gpu=True, gpu_types=["H100"])
    ) != compute_request_hash(_submission(requires_gpu=True))
