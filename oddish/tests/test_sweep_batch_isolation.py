from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import oddish.core.endpoints.sweep as sweep_mod  # noqa: E402
import oddish.core.sweeps as sweeps_mod  # noqa: E402
from oddish.core.endpoints.sweep import create_task_sweep_batch_core  # noqa: E402


class _FakeSavepoint:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def begin_nested(self):
        return _FakeSavepoint()


@pytest.mark.asyncio
async def test_pre_loop_resolution_failure_fails_only_that_item(monkeypatch):
    created: list[str | None] = []

    async def fake_create(session, *, submission, org_id, billed_user_id, **kwargs):
        created.append(billed_user_id)
        raise HTTPException(status_code=404, detail="stop after recording")

    monkeypatch.setattr(sweep_mod, "create_task_sweep_core", fake_create)
    monkeypatch.setattr(sweeps_mod, "validate_sweep_submission", lambda _s: None)

    good, bad = SimpleNamespace(), SimpleNamespace()

    async def resolve(session, submission):
        if submission is bad:
            raise HTTPException(status_code=403, detail={"message": "unlinked"})
        return "amy"

    results = await create_task_sweep_batch_core(
        _FakeSession(),
        submissions=[bad, good],
        org_id="org-1",
        resolve_billed_user_id=resolve,
    )

    assert created == ["amy"]
    assert results[0].success is False
    assert results[0].status_code == 403
    assert results[0].error == "unlinked"
    assert results[1].status_code == 404
