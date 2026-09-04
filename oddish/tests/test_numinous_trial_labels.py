"""Numinous sandboxes are attributable: experiment/trial/agent labels at create,
reward + status stamped at END. Metadata never fails a trial."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from oddish.workers.harbor import runner as harbor_runner
from oddish.workers.queue import trial_handler


def test_numinous_trial_labels_merge_and_drop_empty():
    labels = harbor_runner._numinous_trial_labels(
        existing={"custom": "keep", "gone": None},
        trial_id="zstd-decoder-b53934e2-90",
        agent="codex",
        model="gpt-5.6-sol",
        experiment_id="8d2e9470",
        experiment_name="SWE marathon",
        org_id="org_1",
        trial_kind=None,
    )
    assert labels == {
        "custom": "keep",
        "oddish.experiment_id": "8d2e9470",
        "oddish.experiment_name": "SWE marathon",
        "oddish.trial_id": "zstd-decoder-b53934e2-90",
        "oddish.agent": "codex",
        "oddish.model": "gpt-5.6-sol",
        "oddish.org_id": "org_1",
    }


def test_numinous_trial_labels_without_experiment():
    labels = harbor_runner._numinous_trial_labels(
        existing=None,
        trial_id="t1",
        agent="oracle",
        model=None,
        experiment_id=None,
        experiment_name=None,
        org_id=None,
        trial_kind="agent",
    )
    assert labels == {
        "oddish.trial_id": "t1",
        "oddish.agent": "oracle",
        "oddish.kind": "agent",
    }


def test_stamp_sandbox_outcome_uses_live_environment_when_present():
    calls: list[dict] = []
    ev = SimpleNamespace(
        environment=SimpleNamespace(set_labels=calls.append),
        environment_provider="numinous",
        environment_external_id="sbx_1",
    )
    asyncio.run(trial_handler._stamp_sandbox_outcome(ev, reward=1.0, status="success"))
    assert calls == [{"oddish.status": "success", "oddish.reward": "1.0"}]


def test_stamp_sandbox_outcome_falls_back_to_backend_by_external_id(monkeypatch):
    """Out-of-process harbor forwards only provider + external id (no environment
    object). Caught live on run 2 smoke: the stamp silently no-op'd."""
    stamped: list[tuple[str, dict]] = []

    class _Backend:
        async def set_labels(self, external_id, labels):
            stamped.append((external_id, labels))
            return True

    monkeypatch.setattr(
        trial_handler,
        "get_backend",
        lambda name: _Backend() if name == "numinous" else None,
    )
    ev = SimpleNamespace(
        environment=None,
        environment_provider="numinous",
        environment_external_id="sbx_7",
    )
    asyncio.run(trial_handler._stamp_sandbox_outcome(ev, reward=None, status="failed"))
    assert stamped == [("sbx_7", {"oddish.status": "failed", "oddish.reward": None})]


def test_stamp_sandbox_outcome_ignores_other_providers_and_errors(monkeypatch):
    monkeypatch.setattr(trial_handler, "get_backend", lambda name: None)
    asyncio.run(
        trial_handler._stamp_sandbox_outcome(
            SimpleNamespace(
                environment=None,
                environment_provider="modal",
                environment_external_id="x",
            ),
            reward=1.0,
            status="success",
        )
    )
    asyncio.run(
        trial_handler._stamp_sandbox_outcome(
            SimpleNamespace(
                environment=None,
                environment_provider=None,
                environment_external_id=None,
            ),
            reward=1.0,
            status="success",
        )
    )

    def boom(_labels):
        raise RuntimeError("control plane down")

    asyncio.run(
        trial_handler._stamp_sandbox_outcome(
            SimpleNamespace(
                environment=SimpleNamespace(set_labels=boom),
                environment_provider="numinous",
                environment_external_id=None,
            ),
            reward=0.0,
            status="success",
        )
    )
