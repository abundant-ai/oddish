"""Numinous sandboxes are attributable: experiment/trial/agent labels at create,
reward + status stamped at END. Metadata never fails a trial."""

from __future__ import annotations

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
        trial_kind="agent",
    )
    assert labels == {
        "custom": "keep",
        "oddish.experiment_id": "8d2e9470",
        "oddish.experiment_name": "SWE marathon",
        "oddish.trial_id": "zstd-decoder-b53934e2-90",
        "oddish.agent": "codex",
        "oddish.model": "gpt-5.6-sol",
        "oddish.org_id": "org_1",
        "oddish.kind": "agent",
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


def test_stamp_sandbox_outcome_calls_set_labels():
    calls: list[dict] = []
    env = SimpleNamespace(set_labels=calls.append)
    trial_handler._stamp_sandbox_outcome(env, reward=1.0, status="success")
    assert calls == [{"oddish.status": "success", "oddish.reward": "1.0"}]
    trial_handler._stamp_sandbox_outcome(env, reward=None, status="failed")
    assert calls[-1] == {"oddish.status": "failed", "oddish.reward": None}


def test_stamp_sandbox_outcome_ignores_other_environments_and_errors():
    trial_handler._stamp_sandbox_outcome(
        SimpleNamespace(), reward=1.0, status="success"
    )
    trial_handler._stamp_sandbox_outcome(None, reward=1.0, status="success")

    def boom(_labels):
        raise RuntimeError("control plane down")

    trial_handler._stamp_sandbox_outcome(
        SimpleNamespace(set_labels=boom), reward=0.0, status="success"
    )
