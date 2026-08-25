from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from api.routers import admin
from api.app import create_app
from auth import AuthContext, AuthMethod, require_auth
from models import APIKeyScope
from oddish.core.qa_feedback_export import (
    QaFeedbackExportResponse,
    build_qa_feedback_export_statement,
    export_qa_feedback_core,
)


def test_export_statement_ties_votes_to_the_current_qa_run():
    sql = str(
        build_qa_feedback_export_statement(org_id="org_1", limit=300).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "feedback.target = 'qa_verdict'" in sql
    assert "feedback.created_at >= trials.analysis_finished_at" in sql
    assert "feedback.target_key = (trials.analysis ->> 'classification')" in sql
    assert "trials.trajectory_summary ->> 'schema_version'" in sql
    assert "count(distinct(reviewed.human_vote))" in sql.lower()
    assert "reviewed.review_rank = 1" in sql
    assert "grader.kind = 'qa'" in sql
    assert "LIMIT 300" in sql


class _Mappings:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return _Mappings(self._rows)


class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _Result(self.rows)


@pytest.mark.asyncio
async def test_export_core_serializes_rows_and_total():
    reviewed_at = datetime(2026, 8, 25, tzinfo=timezone.utc)
    session = _Session(
        [
            {
                "trial_id": "task-1",
                "grader_trial_id": "task-9",
                "task_id": "task",
                "task_version_id": "task-v1",
                "experiment_id": "exp",
                "classification": "GOOD_SUCCESS",
                "human_vote": "agree",
                "review_note": "",
                "reviewed_at": reviewed_at,
                "vote_count": 2,
                "reward": 1.0,
                "solver_agent": "codex",
                "solver_model": "openai/gpt-5",
                "judge_agent": "claude-code",
                "judge_model": "anthropic/claude-sonnet-4-6",
                "eligible_total": 417,
            }
        ]
    )

    result = await export_qa_feedback_core(session, org_id="org_1", limit=300)

    assert result.requested_limit == 300
    assert result.eligible_total == 417
    assert result.returned_count == 1
    assert result.items[0].grader_trial_id == "task-9"
    assert result.items[0].vote_count == 2


@pytest.mark.asyncio
async def test_admin_route_requires_operator_org(monkeypatch):
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", "org_operator")
    with pytest.raises(HTTPException) as exc_info:
        await admin.get_qa_feedback_export(
            auth=SimpleNamespace(org_id="org_tenant"), limit=300
        )
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_admin_route_passes_active_org_and_limit(monkeypatch):
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", "org_operator")
    expected = QaFeedbackExportResponse(
        requested_limit=12,
        eligible_total=0,
        returned_count=0,
        items=[],
    )
    seen = {}

    @asynccontextmanager
    async def fake_session():
        yield object()

    async def fake_export(session, *, org_id, limit):
        seen.update(session=session, org_id=org_id, limit=limit)
        return expected

    monkeypatch.setattr(admin, "get_session", fake_session)
    monkeypatch.setattr(admin, "export_qa_feedback_core", fake_export)

    result = await admin.get_qa_feedback_export(
        auth=SimpleNamespace(org_id="org_operator"), limit=12
    )

    assert result is expected
    assert seen["org_id"] == "org_operator"
    assert seen["limit"] == 12


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "expected_status"),
    [(APIKeyScope.TASKS, 403), (APIKeyScope.FULL, 200)],
)
async def test_http_route_requires_full_scope_api_key(
    monkeypatch, scope, expected_status
):
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", "org_operator")
    auth = AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org_operator",
        scope=scope,
    )
    expected = QaFeedbackExportResponse(
        requested_limit=1,
        eligible_total=0,
        returned_count=0,
        items=[],
    )

    @asynccontextmanager
    async def fake_session():
        yield object()

    async def fake_export(session, *, org_id, limit):
        return expected

    monkeypatch.setattr(admin, "get_session", fake_session)
    monkeypatch.setattr(admin, "export_qa_feedback_core", fake_export)

    app = create_app()
    app.dependency_overrides[require_auth] = lambda: auth
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/admin/qa-feedback-export", params={"limit": 1}
            )
    finally:
        app.dependency_overrides.pop(require_auth, None)

    assert response.status_code == expected_status, response.text
