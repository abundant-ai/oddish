from __future__ import annotations

from contextlib import asynccontextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from auth import require_auth
from auth.types import AuthContext, AuthMethod
from models import APIKeyScope
from oddish.schemas import QAEvalCreateResponse, QAEvalTrialResponse

_ROUTER_PATH = Path(__file__).resolve().parents[1] / "api" / "routers" / "qa_eval.py"
_SPEC = spec_from_file_location("qa_eval_route_under_test", _ROUTER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
qa_eval = module_from_spec(_SPEC)
_SPEC.loader.exec_module(qa_eval)


@pytest.mark.asyncio
async def test_post_qa_evals_passes_org_and_owner_to_shared_core(monkeypatch):
    captured: dict = {}

    class FakeSession:
        committed = False

        async def commit(self):
            self.committed = True

    session = FakeSession()

    @asynccontextmanager
    async def fake_get_session():
        yield session

    async def fake_create_core(
        _session,
        *,
        request,
        org_id: str | None,
        owner_user_id: str | None,
        billed_user_id: str | None,
        idempotency_key,
        idempotency_store,
        request_hash,
    ):
        captured.update(
            request=request,
            org_id=org_id,
            owner_user_id=owner_user_id,
            billed_user_id=billed_user_id,
            idempotency_key=idempotency_key,
            idempotency_store=idempotency_store,
            request_hash=request_hash,
        )
        return QAEvalCreateResponse(
            experiment_id="experiment-1",
            experiment_name=request.name,
            prompt_name=request.prompt_name,
            prompt_sha256="abc",
            model="canonical-model",
            trials=[
                QAEvalTrialResponse(
                    source_trial_id="source-1", qa_eval_trial_id="eval-1"
                )
            ],
        )

    monkeypatch.setattr(qa_eval, "get_session", fake_get_session)
    monkeypatch.setattr(qa_eval, "create_qa_eval_core", fake_create_core)
    monkeypatch.setattr(qa_eval, "invalidate_dashboard_cache", lambda **_: None)

    app = FastAPI()
    app.include_router(qa_eval.router)
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        method=AuthMethod.CLERK_JWT,
        org_id="org-1",
        user_id="user-1",
        scope=APIKeyScope.TASKS,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/qa-evals",
            headers={"Idempotency-Key": "stable-key"},
            json={
                "name": "candidate experiment",
                "source_trial_ids": ["source-1"],
                "prompt_name": "candidate-1",
                "prompt_text": "Classify the source.",
            },
        )

    assert response.status_code == 200, response.text
    assert captured["org_id"] == "org-1"
    assert captured["owner_user_id"] == "user-1"
    assert captured["billed_user_id"] == "user-1"
    assert captured["request"].source_trial_ids == ["source-1"]
    assert captured["idempotency_key"] == "stable-key"
    assert captured["request_hash"]
    assert session.committed is True
