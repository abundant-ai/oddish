from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers import cc_chat as cc_chat_router
from api.services.cc_chat.orchestrator import SessionNotFound


class _FakeReg:
    def __init__(self, has_session: bool = True) -> None:
        self.has_session = has_session

    def get(self, sid):
        return object() if self.has_session else None


class FakeOrchestrator:
    def __init__(self) -> None:
        self.start_calls: list[tuple[str, str]] = []
        self.send_calls: list[tuple[str, str]] = []
        self.close_calls: list[str] = []
        self.next_session_id = "sid-test"
        self.canned_events: list[dict] = []
        self._sessions = _FakeReg(has_session=True)

    async def start(self, *, experiment_id: str, org_id: str) -> str:
        self.start_calls.append((experiment_id, org_id))
        return self.next_session_id

    async def send(self, *, session_id: str, content: str):
        self.send_calls.append((session_id, content))
        for ev in self.canned_events:
            yield ev

    async def close(self, *, session_id: str) -> None:
        self.close_calls.append(session_id)


@pytest.fixture
def app_with_fake(monkeypatch):
    fake = FakeOrchestrator()

    def get_orch_override():
        return fake

    monkeypatch.setattr(
        cc_chat_router, "get_orchestrator", get_orch_override
    )

    # Bypass auth: the router takes auth via Depends(require_auth);
    # we override the dep here.
    from auth import AuthContext, AuthMethod
    from models import APIKeyScope

    def fake_auth():
        return AuthContext(
            method=AuthMethod.API_KEY,
            org_id="org-1",
            user_id="user-1",
            scope=APIKeyScope.FULL,
        )

    app = FastAPI()
    app.include_router(cc_chat_router.router)

    from auth import require_auth
    app.dependency_overrides[require_auth] = fake_auth
    return app, fake


def test_post_session_returns_session_id(app_with_fake):
    app, fake = app_with_fake
    client = TestClient(app)
    r = client.post("/api/experiments/exp-1/cc-session")
    assert r.status_code == 200
    assert r.json() == {"session_id": "sid-test"}
    assert fake.start_calls == [("exp-1", "org-1")]


def test_post_message_streams_sse(app_with_fake):
    app, fake = app_with_fake
    fake.canned_events = [
        {"type": "system", "subtype": "init", "session_id": "cc-uuid-1"},
        {"type": "assistant", "delta": "hi"},
        {"type": "result"},
    ]
    client = TestClient(app)
    with client.stream(
        "POST",
        "/api/experiments/exp-1/cc-session/sid-test/messages",
        json={"content": "what failed?"},
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    assert "event: message" in body
    assert "event: done" in body
    # And the orchestrator was actually called
    assert fake.send_calls == [("sid-test", "what failed?")]


def test_post_message_unknown_session_404(app_with_fake):
    app, fake = app_with_fake
    fake._sessions = _FakeReg(has_session=False)

    client = TestClient(app)
    r = client.post(
        "/api/experiments/exp-1/cc-session/missing/messages",
        json={"content": "x"},
    )
    assert r.status_code == 404


def test_delete_session_calls_close(app_with_fake):
    app, fake = app_with_fake
    client = TestClient(app)
    r = client.delete("/api/experiments/exp-1/cc-session/sid-test")
    assert r.status_code == 204
    assert fake.close_calls == ["sid-test"]
