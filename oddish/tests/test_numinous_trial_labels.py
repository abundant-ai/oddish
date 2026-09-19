"""Numinous sandboxes are attributable: experiment/trial/agent labels at create,
reward + status stamped at END. Metadata never fails a trial."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from oddish.workers.harbor import runner as harbor_runner
from oddish.workers.queue import trial_handler


def test_numinous_trial_labels_merge_and_drop_empty(monkeypatch):
    monkeypatch.setenv("MODAL_APP_NAME", "oddish-pr-1425")
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
        "oddish.deployment": "oddish-pr-1425",
        "oddish.experiment_id": "8d2e9470",
        "oddish.experiment_name": "SWE marathon",
        "oddish.trial_id": "zstd-decoder-b53934e2-90",
        "oddish.agent": "codex",
        "oddish.model": "gpt-5.6-sol",
        "oddish.org_id": "org_1",
    }


def test_numinous_trial_labels_without_experiment(monkeypatch):
    monkeypatch.delenv("MODAL_APP_NAME", raising=False)
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


def test_stamp_trial_outcome_on_provider_routes_to_backend(monkeypatch):
    """The stamp that actually runs: after the trial returns, by trial id."""
    from harbor.models.environment_type import EnvironmentType

    calls: list[dict] = []

    class _Backend:
        async def stamp_trial_outcome(self, trial_id, *, reward, status, error=None):
            calls.append(
                {
                    "trial_id": trial_id,
                    "reward": reward,
                    "status": status,
                    "error": error,
                }
            )
            return 1

    monkeypatch.setattr(
        trial_handler,
        "get_backend",
        lambda name: _Backend() if name == "numinous" else None,
    )
    ok = SimpleNamespace(reward=1.0, error=None)
    asyncio.run(
        trial_handler._stamp_trial_outcome_on_provider(
            EnvironmentType.NUMINOUS, trial_id="t-1", outcome=ok, execution_error=None
        )
    )
    bad = SimpleNamespace(reward=None, error="verifier crashed")
    asyncio.run(
        trial_handler._stamp_trial_outcome_on_provider(
            EnvironmentType.NUMINOUS, trial_id="t-2", outcome=bad, execution_error=None
        )
    )
    asyncio.run(
        trial_handler._stamp_trial_outcome_on_provider(
            EnvironmentType.NUMINOUS,
            trial_id="t-3",
            outcome=None,
            execution_error="RuntimeError: boom",
        )
    )
    asyncio.run(
        trial_handler._stamp_trial_outcome_on_provider(
            EnvironmentType.MODAL, trial_id="t-4", outcome=ok, execution_error=None
        )
    )
    assert calls == [
        {"trial_id": "t-1", "reward": 1.0, "status": "success", "error": None},
        {
            "trial_id": "t-2",
            "reward": None,
            "status": "failed",
            "error": "verifier crashed",
        },
        {
            "trial_id": "t-3",
            "reward": None,
            "status": "failed",
            "error": "RuntimeError: boom",
        },
    ]


def test_backend_stamp_trial_outcome_looks_up_by_label_and_patches(monkeypatch):
    import httpx

    from oddish.runtime.backends.numinous import NuminousBackend

    seen: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.method,
                str(request.url),
                None
                if not request.content
                else __import__("json").loads(request.content),
            )
        )
        if request.method == "GET":
            return httpx.Response(
                200, json={"items": [{"id": "sbx_a"}, {"id": "sbx_b"}], "total": 2}
            )
        return httpx.Response(200, json={"id": "x", "labels": {}})

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw)
    )
    monkeypatch.setenv("NUMINOUS_API_URL", "https://cp.test")
    monkeypatch.setenv("NUMINOUS_API_KEY", "nk_test")
    n = asyncio.run(
        NuminousBackend().stamp_trial_outcome("trial-9", reward=0.0, status="success")
    )
    assert n == 2
    assert seen[0][0] == "GET" and "label=oddish.trial_id%3Atrial-9" in seen[0][1]
    assert seen[1] == (
        "PATCH",
        "https://cp.test/v1/sandboxes/sbx_a/labels",
        {"labels": {"oddish.status": "success", "oddish.reward": "0.0"}},
    )
    assert seen[2][1].endswith("/v1/sandboxes/sbx_b/labels")


def test_settle_trial_outcome_on_provider_routes_final_verdict(monkeypatch):
    """Once oddish settles a trial (after retries), the backend gets the final
    verdict: reward + success, or failed. Other providers are untouched;
    an unknown environment is a no-op."""
    from oddish.workers.queue.trial_handler import TrialStatus

    calls: list[dict] = []

    class _Backend:
        async def settle_trial(self, trial_id, *, reward, status):
            calls.append({"trial_id": trial_id, "reward": reward, "status": status})
            return True

    monkeypatch.setattr(
        trial_handler,
        "get_backend",
        lambda name: _Backend() if name == "numinous" else object(),
    )
    asyncio.run(
        trial_handler._settle_trial_outcome_on_provider(
            "numinous", trial_id="t-1", status=TrialStatus.SUCCESS, reward=1.0
        )
    )
    asyncio.run(
        trial_handler._settle_trial_outcome_on_provider(
            "numinous", trial_id="t-2", status=TrialStatus.FAILED, reward=None
        )
    )
    asyncio.run(
        trial_handler._settle_trial_outcome_on_provider(
            "modal", trial_id="t-3", status=TrialStatus.SUCCESS, reward=1.0
        )
    )
    asyncio.run(
        trial_handler._settle_trial_outcome_on_provider(
            "not-an-environment", trial_id="t-4", status=TrialStatus.SUCCESS, reward=1.0
        )
    )
    assert calls == [
        {"trial_id": "t-1", "reward": 1.0, "status": "success"},
        {"trial_id": "t-2", "reward": None, "status": "failed"},
    ]
