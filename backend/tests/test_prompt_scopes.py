"""Scope resolution for the versioned prompt registry.

Mirrors the throwaway-kind pattern in test_prompts_core.py: core is
string-typed, so tests never collide with seeded rows. Cleanup deletes every
scope row for the kind, not just the global one.
"""

import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from oddish.core.prompts import (
    get_prompt_core,
    resolve_prompt_core,
    set_prompt_core,
)
from oddish.db import PromptModel, get_session


@pytest_asyncio.fixture
async def scoped_kind():
    kind = f"test_scope_{uuid.uuid4().hex[:8]}"
    yield kind
    async with get_session() as session:
        await session.execute(
            PromptModel.__table__.delete().where(PromptModel.kind == kind)
        )
        await session.commit()


async def _write(kind, content, *, scope_type=None, scope_id=None, org_id=None):
    async with get_session() as session:
        await set_prompt_core(
            session,
            kind=kind,
            content=content,
            scope_type=scope_type,
            scope_id=scope_id,
            org_id=org_id,
        )
        await session.commit()


_NO_SCOPES = {
    "org_id": None,
    "user_id": None,
    "experiment_id": None,
    "task_id": None,
    "trial_id": None,
}


@pytest.mark.asyncio
async def test_falls_back_to_global_when_no_override(scoped_kind):
    await _write(scoped_kind, "global")
    async with get_session() as session:
        prompt, ver = await resolve_prompt_core(
            session, scoped_kind, **{**_NO_SCOPES, "org_id": "org_a"}
        )
    assert ver.content == "global"
    assert prompt.scope_type is None


@pytest.mark.asyncio
async def test_org_override_beats_global(scoped_kind):
    await _write(scoped_kind, "global")
    await _write(
        scoped_kind, "org", scope_type="org", scope_id="org_a", org_id="org_a"
    )
    async with get_session() as session:
        _, ver = await resolve_prompt_core(
            session, scoped_kind, **{**_NO_SCOPES, "org_id": "org_a"}
        )
    assert ver.content == "org"


@pytest.mark.asyncio
async def test_narrowest_scope_wins_across_all_levels(scoped_kind):
    # Written broad-to-narrow; each addition must take over resolution.
    await _write(scoped_kind, "global")
    ladder = [
        ("org", "org_a", "org"),
        ("user", "user_a", "user"),
        ("experiment", "exp_a", "experiment"),
        ("task", "task_a", "task"),
        ("trial", "trial_a", "trial"),
    ]
    scopes = {**_NO_SCOPES, "org_id": "org_a"}
    for scope_type, scope_id, content in ladder:
        await _write(
            scoped_kind,
            content,
            scope_type=scope_type,
            scope_id=scope_id,
            org_id="org_a",
        )
        scopes[f"{scope_type}_id" if scope_type != "org" else "org_id"] = scope_id
        async with get_session() as session:
            _, ver = await resolve_prompt_core(session, scoped_kind, **scopes)
        assert ver.content == content, f"{scope_type} override did not win"


@pytest.mark.asyncio
async def test_other_orgs_override_is_never_resolved(scoped_kind):
    # The guard that keeps one tenant's prompt out of another's QA.
    await _write(scoped_kind, "global")
    await _write(
        scoped_kind, "org_b_secret",
        scope_type="task", scope_id="task_shared", org_id="org_b",
    )
    async with get_session() as session:
        _, ver = await resolve_prompt_core(
            session,
            scoped_kind,
            **{**_NO_SCOPES, "org_id": "org_a", "task_id": "task_shared"},
        )
    assert ver.content == "global"


@pytest.mark.asyncio
async def test_scope_without_versions_falls_through_to_global(scoped_kind):
    # A prompt row can exist with zero versions; it must not shadow the global.
    await _write(scoped_kind, "global")
    async with get_session() as session:
        session.add(
            PromptModel(
                kind=scoped_kind,
                description="",
                scope_type="task",
                scope_id="task_empty",
                org_id="org_a",
            )
        )
        await session.commit()
    async with get_session() as session:
        _, ver = await resolve_prompt_core(
            session,
            scoped_kind,
            **{**_NO_SCOPES, "org_id": "org_a", "task_id": "task_empty"},
        )
    assert ver.content == "global"


@pytest.mark.asyncio
async def test_resolve_raises_404_when_nothing_exists():
    async with get_session() as session:
        with pytest.raises(HTTPException) as exc:
            await resolve_prompt_core(
                session, "no_such_kind_xyz", **{**_NO_SCOPES, "org_id": "org_a"}
            )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_set_rejects_half_specified_scope(scoped_kind):
    async with get_session() as session:
        with pytest.raises(ValueError):
            await set_prompt_core(
                session, kind=scoped_kind, content="x", scope_type="org"
            )


@pytest.mark.asyncio
async def test_set_rejects_unknown_scope_type(scoped_kind):
    async with get_session() as session:
        with pytest.raises(ValueError):
            await set_prompt_core(
                session,
                kind=scoped_kind,
                content="x",
                scope_type="galaxy",
                scope_id="g1",
            )


@pytest.mark.asyncio
async def test_scoped_and_global_versions_increment_independently(scoped_kind):
    await _write(scoped_kind, "g1")
    await _write(scoped_kind, "g2")
    await _write(
        scoped_kind, "o1", scope_type="org", scope_id="org_a", org_id="org_a"
    )
    async with get_session() as session:
        _, global_ver = await get_prompt_core(session, scoped_kind)
        _, org_ver = await get_prompt_core(
            session, scoped_kind, scope_type="org", scope_id="org_a"
        )
    assert global_ver.version == 2
    assert org_ver.version == 1


@pytest.mark.asyncio
async def test_same_kind_coexists_across_distinct_scopes(scoped_kind):
    await _write(scoped_kind, "global")
    await _write(
        scoped_kind, "a", scope_type="org", scope_id="org_a", org_id="org_a"
    )
    await _write(
        scoped_kind, "b", scope_type="org", scope_id="org_b", org_id="org_b"
    )
    async with get_session() as session:
        _, a = await get_prompt_core(
            session, scoped_kind, scope_type="org", scope_id="org_a"
        )
        _, b = await get_prompt_core(
            session, scoped_kind, scope_type="org", scope_id="org_b"
        )
    assert (a.content, b.content) == ("a", "b")


@pytest.mark.asyncio
async def test_duplicate_row_at_identical_scope_is_rejected(scoped_kind):
    # set_prompt_core appends a version rather than inserting a second row, so
    # the index is exercised by inserting the ORM row directly.
    await _write(
        scoped_kind, "a", scope_type="org", scope_id="org_a", org_id="org_a"
    )
    with pytest.raises(IntegrityError):
        async with get_session() as session:
            session.add(
                PromptModel(
                    kind=scoped_kind,
                    description="",
                    scope_type="org",
                    scope_id="org_a",
                    org_id="org_a",
                )
            )
            await session.commit()
