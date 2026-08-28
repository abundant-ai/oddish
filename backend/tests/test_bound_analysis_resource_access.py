from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from auth.resource_access import authorize_bound_analysis_request
from auth.types import AuthContext, AuthMethod
from models import APIKeyScope
from oddish.db import TaskVersionModel, TrialModel


def _request(
    route_path: str,
    *,
    method: str = "GET",
    path_params: dict[str, str] | None = None,
    query: str = "",
) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": route_path,
            "headers": [],
            "query_string": query.encode(),
            "path_params": path_params or {},
            "route": SimpleNamespace(path=route_path),
        }
    )


def _auth(bound_id: str | None = "analysis-1") -> AuthContext:
    return AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org-1",
        api_key_id="key-1",
        bound_analysis_trial_id=bound_id,
        scope=APIKeyScope.READ,
    )


def _session(monkeypatch, analysis_trial, version=None):
    class FakeSession:
        async def get(self, model, row_id):
            if model is TrialModel and row_id == "analysis-1":
                return analysis_trial
            if (
                model is TaskVersionModel
                and version is not None
                and row_id == version.id
            ):
                return version
            return None

    @asynccontextmanager
    async def fake_get_session():
        yield FakeSession()

    monkeypatch.setattr("auth.resource_access.get_session", fake_get_session)


@pytest.mark.asyncio
async def test_unbound_operator_probe_key_keeps_existing_read_policy(monkeypatch):
    async def unexpected_session():
        raise AssertionError("unbound keys must not query an analysis trial")

    monkeypatch.setattr("auth.resource_access.get_session", unexpected_session)
    await authorize_bound_analysis_request(
        _request("/tasks/{task_id}/detail", path_params={"task_id": "task-2"}),
        _auth(None),
    )


@pytest.mark.asyncio
async def test_qa_key_reads_only_trial_ids_derived_from_analysis_payload(monkeypatch):
    analysis = SimpleNamespace(
        id="analysis-1",
        org_id="org-1",
        kind="qa",
        task_id="task-1",
        task_version_id="version-1",
        harbor_config={"analysis_payload": {"trial_ids": ["source-1", "source-2"]}},
    )
    _session(monkeypatch, analysis)

    await authorize_bound_analysis_request(
        _request(
            "/trials/{trial_id}/trajectory",
            path_params={"trial_id": "source-2"},
        ),
        _auth(),
    )

    with pytest.raises(HTTPException) as denied:
        await authorize_bound_analysis_request(
            _request(
                "/trials/{trial_id}/result",
                path_params={"trial_id": "other-trial"},
            ),
            _auth(),
        )
    assert denied.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route_path",
    (
        "/trials/{trial_id}/files",
        "/trials/{trial_id}/files/{file_path:path}",
        "/trials/{trial_id}/debug-files",
        "/trials/{trial_id}/probe-artifacts",
        "/trials/{trial_id}/live",
        "/trials/{trial_id}/trajectory/summary",
    ),
)
async def test_qa_key_denies_trial_routes_the_prompt_does_not_need(
    monkeypatch, route_path
):
    analysis = SimpleNamespace(
        id="analysis-1",
        org_id="org-1",
        kind="qa",
        task_id="task-1",
        task_version_id="version-1",
        harbor_config={"analysis_payload": {"trial_ids": ["source-1"]}},
    )
    _session(monkeypatch, analysis)

    with pytest.raises(HTTPException) as denied:
        await authorize_bound_analysis_request(
            _request(
                route_path,
                path_params={"trial_id": "source-1", "file_path": "secret.txt"},
            ),
            _auth(),
        )
    assert denied.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route_path",
    (
        "/trials/{trial_id}/result",
        "/trials/{trial_id}/trajectory",
        "/trials/{trial_id}/logs/structured",
    ),
)
async def test_qa_key_allows_only_the_three_evidence_routes(monkeypatch, route_path):
    analysis = SimpleNamespace(
        id="analysis-1",
        org_id="org-1",
        kind="qa",
        task_id="task-1",
        task_version_id="version-1",
        harbor_config={"analysis_payload": {"trial_ids": ["source-1"]}},
    )
    _session(monkeypatch, analysis)

    await authorize_bound_analysis_request(
        _request(route_path, path_params={"trial_id": "source-1"}),
        _auth(),
    )


@pytest.mark.asyncio
async def test_qa_eval_key_rejects_a_payload_with_multiple_source_trials(monkeypatch):
    analysis = SimpleNamespace(
        id="analysis-1",
        org_id="org-1",
        kind="qa_eval",
        task_id="task-1",
        task_version_id="version-1",
        harbor_config={"analysis_payload": {"trial_ids": ["source-1", "source-2"]}},
    )
    _session(monkeypatch, analysis)

    for source_trial_id in ("source-1", "source-2"):
        with pytest.raises(HTTPException) as denied:
            await authorize_bound_analysis_request(
                _request(
                    "/trials/{trial_id}/result",
                    path_params={"trial_id": source_trial_id},
                ),
                _auth(),
            )
        assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_bound_key_denies_non_resource_and_mutating_routes(monkeypatch):
    analysis = SimpleNamespace(
        id="analysis-1",
        org_id="org-1",
        kind="qa_eval",
        task_id="task-1",
        task_version_id="version-1",
        harbor_config={"analysis_payload": {"trial_ids": ["source-1"]}},
    )
    _session(monkeypatch, analysis)

    with pytest.raises(HTTPException):
        await authorize_bound_analysis_request(
            _request("/dashboard"),
            _auth(),
        )
    with pytest.raises(HTTPException):
        await authorize_bound_analysis_request(
            _request(
                "/trials/{trial_id}/retry",
                method="POST",
                path_params={"trial_id": "source-1"},
            ),
            _auth(),
        )


@pytest.mark.asyncio
async def test_audit_key_reads_only_its_pinned_task_version(monkeypatch):
    analysis = SimpleNamespace(
        id="analysis-1",
        org_id="org-1",
        kind="audit",
        task_id="task-1",
        task_version_id="version-7",
        harbor_config={"analysis_payload": {}},
    )
    version = SimpleNamespace(id="version-7", task_id="task-1", version=7)
    _session(monkeypatch, analysis, version)

    await authorize_bound_analysis_request(
        _request(
            "/tasks/{task_id}/files/{file_path:path}",
            path_params={"task_id": "task-1", "file_path": "instruction.md"},
            query="version=7",
        ),
        _auth(),
    )

    for task_id, query in (
        ("task-2", "version=7"),
        ("task-1", "version=8"),
        ("task-1", ""),
    ):
        with pytest.raises(HTTPException):
            await authorize_bound_analysis_request(
                _request(
                    "/tasks/{task_id}/files",
                    path_params={"task_id": task_id},
                    query=query,
                ),
                _auth(),
            )


@pytest.mark.asyncio
async def test_summarize_binding_fails_closed(monkeypatch):
    analysis = SimpleNamespace(
        id="analysis-1",
        org_id="org-1",
        kind="summarize",
        task_id="task-1",
        task_version_id="version-1",
        harbor_config={"analysis_payload": {"trial_ids": ["source-1"]}},
    )
    _session(monkeypatch, analysis)

    with pytest.raises(HTTPException):
        await authorize_bound_analysis_request(
            _request(
                "/trials/{trial_id}/result",
                path_params={"trial_id": "source-1"},
            ),
            _auth(),
        )
