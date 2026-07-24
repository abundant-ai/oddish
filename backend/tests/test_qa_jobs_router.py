"""Tenancy and permission behavior of the /qa-jobs surface.

These run against the real database because the interesting failures are all
row-visibility failures, which fakes cannot reproduce. Every test arranges its
own prompt and assignment state.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from auth import APIKeyScope, AuthContext, AuthMethod, require_auth
from oddish.core.prompts import set_prompt_core
from oddish.db import (
    ExperimentModel,
    PromptModel,
    QAAssignmentModel,
    TaskModel,
    TrialModel,
    get_session,
)
from oddish.db.models import Priority, TaskStatus

OPERATOR_ORG = "org_operator"


def _auth(*, org_id="org_a", user_id="u1", scope=APIKeyScope.FULL):
    def _factory():
        return AuthContext(
            method=AuthMethod.API_KEY, org_id=org_id, user_id=user_id, scope=scope
        )

    return _factory


async def _client(**auth_kw):
    app = create_app()
    app.dependency_overrides[require_auth] = _auth(**auth_kw)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest_asyncio.fixture
async def kind():
    value = f"test_qajobs_{uuid.uuid4().hex[:8]}"
    yield value
    async with get_session() as session:
        rows = (
            await session.execute(
                PromptModel.__table__.select().where(PromptModel.kind == value)
            )
        ).all()
        for row in rows:
            await session.execute(
                QAAssignmentModel.__table__.delete().where(
                    QAAssignmentModel.prompt_id == row.id
                )
            )
        await session.execute(
            PromptModel.__table__.delete().where(PromptModel.kind == value)
        )
        await session.commit()


@pytest_asyncio.fixture
async def task():
    """A real task row -- resolve_write_scope verifies domain ids exist."""
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    async with get_session() as session:
        session.add(
            TaskModel(
                id=task_id,
                name=task_id,
                user="tester",
                org_id="org_a",
                priority=Priority.LOW,
                status=TaskStatus.PENDING,
                task_path="/tmp/task",
            )
        )
        await session.commit()
    yield task_id
    async with get_session() as session:
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == task_id)
        )
        await session.commit()


@pytest_asyncio.fixture
async def trial(task):
    """A real trial under an experiment and the `task` fixture -- so resolving
    at trial scope has a task/experiment ancestry to walk."""
    exp_id = f"exp_{uuid.uuid4().hex[:8]}"
    trial_id = f"{task}-0"
    async with get_session() as session:
        session.add(ExperimentModel(id=exp_id, name=exp_id, org_id="org_a"))
        await session.flush()
        session.add(
            TrialModel(
                id=trial_id,
                name=trial_id,
                task_id=task,
                experiment_id=exp_id,
                org_id="org_a",
                agent="claude-code",
                provider="anthropic",
                queue_key="q",
            )
        )
        await session.commit()
    yield {"trial_id": trial_id, "experiment_id": exp_id, "task_id": task}
    async with get_session() as session:
        await session.execute(
            TrialModel.__table__.delete().where(TrialModel.id == trial_id)
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(ExperimentModel.id == exp_id)
        )
        await session.commit()


async def _make_prompt(kind, *, scope_type=None, scope_id=None, org_id=None):
    async with get_session() as session:
        version = await set_prompt_core(
            session,
            kind=kind,
            content="body",
            scope_type=scope_type,
            scope_id=scope_id,
            org_id=org_id,
        )
        prompt_id = version.prompt_id
        await session.commit()
    return prompt_id


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_then_resolve_at_task_scope(kind, task):
    await _make_prompt(kind, scope_type="task", scope_id=task, org_id="org_a")
    async with await _client() as client:
        created = await client.post(
            "/qa-jobs",
            params={"scope": "task", "scope_id": task},
            json={"prompt": kind, "stage": "post_trial", "model": "claude-opus-4-8"},
        )
        assert created.status_code == 200, created.text
        assert created.json()["inherited_from"] == f"task:{task}"

        listed = await client.get(
            "/qa-jobs",
            params={"scope": "task", "scope_id": task, "resolved": "true"},
        )
    assert listed.status_code == 200
    mine = [r for r in listed.json() if r["prompt_kind"] == kind]
    assert len(mine) == 1
    assert mine[0]["model"] == "claude-opus-4-8"
    assert mine[0]["backend"] == "api"


@pytest.mark.asyncio
async def test_stage_defaults_pick_the_matching_runner(kind, task):
    """`model` is NOT NULL, so a request that omits it must still produce a
    valid row -- that is what makes a bare `qa-jobs disable` possible."""
    await _make_prompt(kind, scope_type="task", scope_id=task, org_id="org_a")
    async with await _client() as client:
        pre = await client.post(
            "/qa-jobs",
            params={"scope": "task", "scope_id": task},
            json={"prompt": kind, "stage": "pre_trial"},
        )
    assert pre.status_code == 200, pre.text
    body = pre.json()
    assert body["backend"] == "sandbox"
    assert body["model"]


@pytest.mark.asyncio
async def test_disable_preserves_the_existing_runner_config(kind, task):
    """`qa-jobs disable` sends only enabled=false. It must flip the flag on a
    configured row without resetting its model/backend/CLI/version to stage
    defaults -- otherwise re-enabling silently loses the runner settings."""
    await _make_prompt(kind, scope_type="task", scope_id=task, org_id="org_a")
    async with await _client() as client:
        assigned = await client.post(
            "/qa-jobs",
            params={"scope": "task", "scope_id": task},
            json={
                "prompt": kind,
                "stage": "post_trial",
                "model": "claude-opus-4-8",
                "backend": "sandbox",
                "allow_oddish_cli": True,
                "prompt_version": 1,
            },
        )
        assert assigned.status_code == 200, assigned.text

        # The bare disable payload the CLI sends -- no runner config.
        disabled = await client.post(
            "/qa-jobs",
            params={"scope": "task", "scope_id": task},
            json={"prompt": kind, "stage": "post_trial", "enabled": False},
        )
        assert disabled.status_code == 200, disabled.text

        listed = await client.get(
            "/qa-jobs",
            params={"scope": "task", "scope_id": task, "resolved": "false"},
        )
    assert listed.status_code == 200
    mine = [r for r in listed.json() if r["prompt_kind"] == kind]
    assert len(mine) == 1
    row = mine[0]
    assert row["enabled"] is False
    assert row["model"] == "claude-opus-4-8"
    assert row["backend"] == "sandbox"
    assert row["allow_oddish_cli"] is True
    assert row["prompt_version"] == 1


@pytest.mark.asyncio
async def test_reenable_via_bare_assign_preserves_config(kind, task):
    """Re-enabling a disabled job is a bare `assign` (enabled defaults true, no
    runner flags). It must restore the flag without wiping the config that
    `disable` deliberately preserved -- the round-trip has to be lossless."""
    await _make_prompt(kind, scope_type="task", scope_id=task, org_id="org_a")
    async with await _client() as client:
        await client.post(
            "/qa-jobs",
            params={"scope": "task", "scope_id": task},
            json={
                "prompt": kind,
                "stage": "post_trial",
                "model": "claude-opus-4-8",
                "backend": "sandbox",
                "allow_oddish_cli": True,
                "prompt_version": 1,
            },
        )
        await client.post(
            "/qa-jobs",
            params={"scope": "task", "scope_id": task},
            json={"prompt": kind, "stage": "post_trial", "enabled": False},
        )
        # Bare re-enable: no runner fields, enabled defaults to true.
        reenabled = await client.post(
            "/qa-jobs",
            params={"scope": "task", "scope_id": task},
            json={"prompt": kind, "stage": "post_trial"},
        )
        assert reenabled.status_code == 200, reenabled.text
    row = reenabled.json()
    assert row["enabled"] is True
    assert row["model"] == "claude-opus-4-8"
    assert row["backend"] == "sandbox"
    assert row["allow_oddish_cli"] is True
    assert row["prompt_version"] == 1


@pytest.mark.asyncio
async def test_assign_updates_only_the_provided_runner_fields(kind, task):
    """A partial `assign --model X` rewrites only the model; backend/CLI/version
    it did not mention stay put."""
    await _make_prompt(kind, scope_type="task", scope_id=task, org_id="org_a")
    async with await _client() as client:
        await client.post(
            "/qa-jobs",
            params={"scope": "task", "scope_id": task},
            json={
                "prompt": kind,
                "stage": "post_trial",
                "model": "claude-opus-4-8",
                "backend": "sandbox",
                "allow_oddish_cli": True,
                "prompt_version": 1,
            },
        )
        updated = await client.post(
            "/qa-jobs",
            params={"scope": "task", "scope_id": task},
            json={"prompt": kind, "stage": "post_trial", "model": "claude-haiku-4-5"},
        )
        assert updated.status_code == 200, updated.text
    row = updated.json()
    assert row["model"] == "claude-haiku-4-5"
    assert row["backend"] == "sandbox"
    assert row["allow_oddish_cli"] is True
    assert row["prompt_version"] == 1


@pytest.mark.asyncio
async def test_resolved_at_trial_includes_ancestor_task_jobs(kind, trial):
    """`resolved` at a trial must union in the jobs defined on its parent task
    (and experiment/org) -- otherwise list/status under-report what actually
    runs, which is keyed off the trial's full ancestry at enqueue time."""
    await _make_prompt(
        kind, scope_type="task", scope_id=trial["task_id"], org_id="org_a"
    )
    async with await _client() as client:
        assigned = await client.post(
            "/qa-jobs",
            params={"scope": "task", "scope_id": trial["task_id"]},
            json={"prompt": kind, "stage": "post_trial"},
        )
        assert assigned.status_code == 200, assigned.text
        resolved = await client.get(
            "/qa-jobs",
            params={
                "scope": "trial",
                "scope_id": trial["trial_id"],
                "resolved": "true",
            },
        )
    assert resolved.status_code == 200, resolved.text
    mine = [r for r in resolved.json() if r["prompt_kind"] == kind]
    assert len(mine) == 1
    assert mine[0]["inherited_from"] == f"task:{trial['task_id']}"


@pytest.mark.asyncio
async def test_defined_listing_excludes_inherited_rows(kind, task):
    # Assign at org scope, then confirm the task scope defines nothing itself.
    await _make_prompt(kind, scope_type="org", scope_id="org_a", org_id="org_a")
    async with await _client() as client:
        assigned = await client.post(
            "/qa-jobs",
            params={"scope": "org"},
            json={"prompt": kind, "stage": "post_trial"},
        )
        assert assigned.status_code == 200, assigned.text
        resolved = await client.get(
            "/qa-jobs", params={"scope": "task", "scope_id": task, "resolved": "true"}
        )
        defined = await client.get(
            "/qa-jobs", params={"scope": "task", "scope_id": task, "resolved": "false"}
        )
    assert kind in {r["prompt_kind"] for r in resolved.json()}
    assert kind not in {r["prompt_kind"] for r in defined.json()}


# ---------------------------------------------------------------------------
# Tenancy -- an id names any row in the installation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cannot_assign_another_orgs_prompt_by_id(kind):
    """`prompt` accepts an opaque id, and id lookup is deliberately unscoped,
    so without a re-check this binds an assignment to a foreign org's row."""
    foreign_id = await _make_prompt(
        kind, scope_type="org", scope_id="org_b", org_id="org_b"
    )
    async with await _client(org_id="org_a") as client:
        resp = await client.post(
            "/qa-jobs",
            params={"scope": "org"},
            json={"prompt": foreign_id, "stage": "post_trial"},
        )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_cannot_delete_another_orgs_assignment(kind):
    await _make_prompt(kind, scope_type="org", scope_id="org_b", org_id="org_b")
    async with await _client(org_id="org_b") as client:
        created = await client.post(
            "/qa-jobs",
            params={"scope": "org"},
            json={"prompt": kind, "stage": "post_trial"},
        )
        assert created.status_code == 200, created.text
        assignment_id = created.json()["id"]

    async with await _client(org_id="org_a") as client:
        resp = await client.delete(f"/qa-jobs/{assignment_id}")
    assert resp.status_code == 404

    # The owner can still delete it -- proving the 404 was the guard, not a
    # missing row.
    async with await _client(org_id="org_b") as client:
        owner = await client.delete(f"/qa-jobs/{assignment_id}")
    assert owner.status_code == 200


@pytest.mark.asyncio
async def test_another_orgs_assignment_absent_from_resolution(kind, task):
    await _make_prompt(kind, scope_type="task", scope_id=task, org_id="org_a")
    async with await _client(org_id="org_a") as client:
        created = await client.post(
            "/qa-jobs",
            params={"scope": "task", "scope_id": task},
            json={"prompt": kind, "stage": "post_trial"},
        )
        assert created.status_code == 200, created.text

    async with await _client(org_id="org_b") as client:
        resp = await client.get(
            "/qa-jobs", params={"scope": "task", "scope_id": task, "resolved": "true"}
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Global scope is installation-wide, so FULL is not sufficient
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_assign_rejected_for_non_operator(kind, monkeypatch):
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", OPERATOR_ORG)
    await _make_prompt(kind)
    async with await _client(org_id="org_a") as client:
        resp = await client.post(
            "/qa-jobs",
            params={"scope": "global"},
            json={"prompt": kind, "stage": "post_trial"},
        )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_global_assign_and_delete_allowed_for_operator(kind, monkeypatch):
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", OPERATOR_ORG)
    await _make_prompt(kind)
    async with await _client(org_id=OPERATOR_ORG) as client:
        created = await client.post(
            "/qa-jobs",
            params={"scope": "global"},
            json={"prompt": kind, "stage": "post_trial"},
        )
        assert created.status_code == 200, created.text
        assert created.json()["org_id"] is None
        assignment_id = created.json()["id"]

    # A different tenant with FULL must not be able to delete the shared row.
    async with await _client(org_id="org_a") as client:
        blocked = await client.delete(f"/qa-jobs/{assignment_id}")
    assert blocked.status_code == 403, blocked.text

    async with await _client(org_id=OPERATOR_ORG) as client:
        allowed = await client.delete(f"/qa-jobs/{assignment_id}")
    assert allowed.status_code == 200


@pytest.mark.asyncio
async def test_global_assign_by_kind_binds_the_installation_wide_prompt(
    kind, monkeypatch
):
    """A global assign must resolve the kind with no org context. The operator
    has its own org-scoped override for this kind; binding *that* private row
    into an installation-wide assignment would leak it to every tenant."""
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", OPERATOR_ORG)
    global_id = await _make_prompt(kind)
    operator_override_id = await _make_prompt(
        kind, scope_type="org", scope_id=OPERATOR_ORG, org_id=OPERATOR_ORG
    )
    assert global_id != operator_override_id

    async with await _client(org_id=OPERATOR_ORG) as client:
        created = await client.post(
            "/qa-jobs",
            params={"scope": "global"},
            json={"prompt": kind, "stage": "post_trial"},
        )
    assert created.status_code == 200, created.text
    assert created.json()["prompt_id"] == global_id
    assert created.json()["prompt_id"] != operator_override_id


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_requires_full_scope(kind):
    await _make_prompt(kind, scope_type="org", scope_id="org_a", org_id="org_a")
    async with await _client(scope=APIKeyScope.READ) as client:
        resp = await client.post(
            "/qa-jobs",
            params={"scope": "org"},
            json={"prompt": kind, "stage": "post_trial"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_prompt_with_no_versions_is_rejected_at_assign_time(kind):
    async with get_session() as session:
        session.add(
            PromptModel(
                kind=kind,
                description="",
                scope_type="org",
                scope_id="org_a",
                org_id="org_a",
            )
        )
        await session.commit()
    async with await _client() as client:
        resp = await client.post(
            "/qa-jobs",
            params={"scope": "org"},
            json={"prompt": kind, "stage": "post_trial"},
        )
    assert resp.status_code == 400
    assert "no versions" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_pinning_an_unknown_version_is_rejected(kind):
    await _make_prompt(kind, scope_type="org", scope_id="org_a", org_id="org_a")
    async with await _client() as client:
        resp = await client.post(
            "/qa-jobs",
            params={"scope": "org"},
            json={"prompt": kind, "stage": "post_trial", "prompt_version": 99},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_unknown_stage_is_rejected(kind):
    await _make_prompt(kind, scope_type="org", scope_id="org_a", org_id="org_a")
    async with await _client() as client:
        resp = await client.post(
            "/qa-jobs",
            params={"scope": "org"},
            json={"prompt": kind, "stage": "mid_trial"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_unknown_task_scope_is_rejected(kind):
    await _make_prompt(kind)
    async with await _client() as client:
        resp = await client.post(
            "/qa-jobs",
            params={"scope": "task", "scope_id": "task_does_not_exist"},
            json={"prompt": kind, "stage": "post_trial"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_status_rollup_reports_zero_before_any_runs(kind, task):
    await _make_prompt(kind, scope_type="task", scope_id=task, org_id="org_a")
    async with await _client() as client:
        created = await client.post(
            "/qa-jobs",
            params={"scope": "task", "scope_id": task},
            json={"prompt": kind, "stage": "post_trial"},
        )
        assert created.status_code == 200, created.text
        resp = await client.get(
            "/qa-jobs/status", params={"scope": "task", "scope_id": task}
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope"] == f"task:{task}"
    row = next(r for r in body["jobs"] if r["prompt_kind"] == kind)
    assert row["total"] == 0
    assert row["counts"] == {}
