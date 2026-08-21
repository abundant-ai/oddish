from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.routers import trials as trial_routes


class _Auth:
    org_id = "org-1"

    def require_scope(self, *_args, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_analysis_output_resolves_qa_artifact_by_stable_kind(monkeypatch):
    trial = SimpleNamespace(id="task-1-9", kind="qa")
    seen: list[str] = []

    async def authorized(_trial_id, _auth):
        return trial

    async def read_bytes(_trial, filename):
        seen.append(filename)
        return b'{"trials": [], "verdict": null}'

    monkeypatch.setattr(trial_routes, "_get_authorized_trial", authorized)
    monkeypatch.setattr(trial_routes, "read_artifact_bytes", read_bytes)

    response = await trial_routes.get_analysis_trial_output(
        "task-1-9", "artifact", _Auth()
    )

    assert seen == ["qa_result.json"]
    assert response.media_type == "application/json"
    assert response.body.startswith(b'{"trials"')


@pytest.mark.asyncio
async def test_analysis_output_rejects_agent_trials(monkeypatch):
    async def authorized(_trial_id, _auth):
        return SimpleNamespace(id="task-1-1", kind="agent")

    monkeypatch.setattr(trial_routes, "_get_authorized_trial", authorized)

    with pytest.raises(HTTPException) as raised:
        await trial_routes.get_analysis_trial_output("task-1-1", "artifact", _Auth())

    assert raised.value.status_code == 404


@pytest.mark.asyncio
async def test_analysis_output_returns_not_found_when_file_is_absent(monkeypatch):
    async def authorized(_trial_id, _auth):
        return SimpleNamespace(id="task-1-9", kind="qa")

    async def missing(_trial, _filename):
        return None

    monkeypatch.setattr(trial_routes, "_get_authorized_trial", authorized)
    monkeypatch.setattr(trial_routes, "read_artifact_bytes", missing)

    with pytest.raises(HTTPException) as raised:
        await trial_routes.get_analysis_trial_output(
            "task-1-9", "validation", _Auth()
        )

    assert raised.value.status_code == 404
