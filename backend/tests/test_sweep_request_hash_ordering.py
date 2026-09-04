"""``POST /tasks/sweep`` fingerprints the client's raw body before mutating it.

``validate_sweep_submission`` rewrites ``config.model`` (curated alias
canonicalization and provider pinning). The route used to run it *before*
``compute_request_hash``, so the recorded idempotency fingerprint described the
server's rewrite rather than what the client sent. Curated resolution is
deterministic now, but the fingerprint must still be taken from the raw body:
it is the client's key, and any future environment-sensitive validation step
would otherwise turn an honest retry into a 409.

These tests drive the real route coroutine and stop it at the validation call,
so they need no database.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import api.routers.tasks as tasks_router
import oddish.core.sweeps as sweeps_mod
from models import APIKeyScope
from oddish.core.idempotency import compute_request_hash
from oddish.schemas import AgentModelPair, TaskSweepSubmission


class _StopAfterValidation(Exception):
    """Sentinel so the route never reaches ``get_session()``."""


def _submission(model: str = "deepseek-v4-flash") -> TaskSweepSubmission:
    return TaskSweepSubmission(
        task_id="task-hash-order",
        experiment_id="exp-1",
        configs=[AgentModelPair(agent="mini-swe-agent", model=model, n_trials=1)],
    )


def _auth() -> SimpleNamespace:
    scopes: list[object] = []
    return SimpleNamespace(
        org_id="org-1",
        user_id="user-1",
        api_key_id=None,
        api_key=None,
        require_scope=scopes.append,
        seen_scopes=scopes,
    )


def _install_probes(monkeypatch, *, mutate_to: str | None):
    """Record call order; optionally have validation rewrite the model."""
    order: list[str] = []
    hashed: list[str] = []
    real_hash = compute_request_hash

    def fake_validate(submission):
        order.append("validate")
        if mutate_to is not None:
            submission.configs[0].model = mutate_to
        raise _StopAfterValidation()

    def recording_hash(submission):
        order.append("hash")
        digest = real_hash(submission)
        hashed.append(digest)
        return digest

    monkeypatch.setattr(sweeps_mod, "validate_sweep_submission", fake_validate)
    monkeypatch.setattr(tasks_router, "compute_request_hash", recording_hash)
    return order, hashed


@pytest.mark.asyncio
async def test_request_hash_is_computed_before_validation(monkeypatch):
    order, _ = _install_probes(monkeypatch, mutate_to=None)

    with pytest.raises(_StopAfterValidation):
        await tasks_router.create_task_sweep(_submission(), _auth(), None)

    assert order == ["hash", "validate"]


@pytest.mark.asyncio
async def test_request_hash_describes_the_raw_body_not_the_rewrite(monkeypatch):
    """The recorded digest matches the client's bytes, not the canonical id."""
    canonical = "fireworks/deepseek-v4-flash-0731"
    _, hashed = _install_probes(monkeypatch, mutate_to=canonical)

    with pytest.raises(_StopAfterValidation):
        await tasks_router.create_task_sweep(_submission(), _auth(), None)

    assert hashed == [compute_request_hash(_submission("deepseek-v4-flash"))]
    assert hashed[0] != compute_request_hash(_submission(canonical))


@pytest.mark.asyncio
async def test_scope_is_enforced_before_any_hashing(monkeypatch):
    """Ordering change must not move work ahead of the scope check."""
    order, _ = _install_probes(monkeypatch, mutate_to=None)
    auth = _auth()

    with pytest.raises(_StopAfterValidation):
        await tasks_router.create_task_sweep(_submission(), auth, None)

    assert auth.seen_scopes == [APIKeyScope.TASKS]


@pytest.mark.asyncio
async def test_two_spellings_of_one_canonical_model_hash_differently(monkeypatch):
    """Raw-body hashing keeps distinct client submissions distinct.

    ``deepseek-v4-flash`` and ``fireworks/deepseek-v4-flash-0731`` resolve to the
    same trial, but they are different requests and must not share an
    idempotency record.
    """
    _, hashed = _install_probes(monkeypatch, mutate_to=None)

    for model in ("deepseek-v4-flash", "fireworks/deepseek-v4-flash-0731"):
        with pytest.raises(_StopAfterValidation):
            await tasks_router.create_task_sweep(_submission(model), _auth(), None)

    assert len(set(hashed)) == 2
