import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException

from oddish.core.prompts import (
    get_latest_prompt_content,
    get_prompt_core,
    list_prompt_versions_core,
    set_prompt_core,
)
from oddish.db import PromptModel, get_session


@pytest_asyncio.fixture
async def prompt_kind():
    # Random throwaway kind: core is deliberately string-typed (enum is
    # enforced at the router), so tests never collide with the seeded rows.
    kind = f"test_prompt_{uuid.uuid4().hex[:8]}"
    yield kind
    async with get_session() as session:
        await session.execute(PromptModel.__table__.delete().where(PromptModel.kind == kind))
        await session.commit()


@pytest.mark.asyncio
async def test_set_creates_v1(prompt_kind):
    async with get_session() as session:
        v = await set_prompt_core(session, kind=prompt_kind, content="hello", description="d")
        await session.commit()
        assert v.version == 1
    async with get_session() as session:
        prompt, ver = await get_prompt_core(session, prompt_kind)
        assert prompt.kind == prompt_kind
        assert ver.version == 1
        assert ver.content == "hello"


@pytest.mark.asyncio
async def test_set_appends_and_latest_wins(prompt_kind):
    async with get_session() as session:
        await set_prompt_core(session, kind=prompt_kind, content="v1")
        await session.commit()
    async with get_session() as session:
        v2 = await set_prompt_core(session, kind=prompt_kind, content="v2")
        await session.commit()
        assert v2.version == 2
    async with get_session() as session:
        assert await get_latest_prompt_content(session, prompt_kind) == "v2"
        versions = await list_prompt_versions_core(session, prompt_kind)
        assert [x.version for x in versions] == [1, 2]


@pytest.mark.asyncio
async def test_get_explicit_version_still_readable(prompt_kind):
    async with get_session() as session:
        await set_prompt_core(session, kind=prompt_kind, content="v1")
        await set_prompt_core(session, kind=prompt_kind, content="v2")
        await session.commit()
    async with get_session() as session:
        _, ver = await get_prompt_core(session, prompt_kind, version=1)
        assert ver.content == "v1"


@pytest.mark.asyncio
async def test_rollback_is_reappend(prompt_kind):
    # "Rolling back" = re-publishing old content as a NEW version; the
    # timeline stays monotonic and the latest is always what runs.
    async with get_session() as session:
        await set_prompt_core(session, kind=prompt_kind, content="good")
        await set_prompt_core(session, kind=prompt_kind, content="bad edit")
        v3 = await set_prompt_core(session, kind=prompt_kind, content="good")
        await session.commit()
        assert v3.version == 3
    async with get_session() as session:
        assert await get_latest_prompt_content(session, prompt_kind) == "good"


@pytest.mark.asyncio
async def test_get_missing_kind_raises_404():
    async with get_session() as session:
        with pytest.raises(HTTPException) as exc:
            await get_prompt_core(session, "does_not_exist_xyz")
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_missing_version_raises_404(prompt_kind):
    async with get_session() as session:
        await set_prompt_core(session, kind=prompt_kind, content="v1")
        await session.commit()
    async with get_session() as session:
        with pytest.raises(HTTPException) as exc:
            await get_prompt_core(session, prompt_kind, version=99)
        assert exc.value.status_code == 404
