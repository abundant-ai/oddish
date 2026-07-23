import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException

from oddish.core.prompts import (
    activate_prompt_version_core,
    get_active_prompt_content,
    get_prompt_core,
    get_prompt_usage_core,
    list_prompt_versions_core,
    set_prompt_core,
)
from oddish.db import JobStatus, PromptModel, get_session
from oddish.db.models import AnalyzerBlockModel


@pytest_asyncio.fixture
async def prompt_key():
    key = f"test_prompt_{uuid.uuid4().hex[:8]}"
    yield key
    async with get_session() as session:
        await session.execute(PromptModel.__table__.delete().where(PromptModel.key == key))
        await session.commit()


@pytest.mark.asyncio
async def test_set_creates_v1_and_activates(prompt_key):
    async with get_session() as session:
        v = await set_prompt_core(session, key=prompt_key, content="hello", description="d")
        await session.commit()
        assert v.version == 1
    async with get_session() as session:
        prompt, ver = await get_prompt_core(session, prompt_key)
        assert prompt.active_version == 1
        assert ver.content == "hello"


@pytest.mark.asyncio
async def test_set_appends_and_bumps_version(prompt_key):
    async with get_session() as session:
        await set_prompt_core(session, key=prompt_key, content="v1")
        await session.commit()
    async with get_session() as session:
        v2 = await set_prompt_core(session, key=prompt_key, content="v2")
        await session.commit()
        assert v2.version == 2
    async with get_session() as session:
        assert await get_active_prompt_content(session, prompt_key) == "v2"
        versions = await list_prompt_versions_core(session, prompt_key)
        assert [x.version for x in versions] == [1, 2]


@pytest.mark.asyncio
async def test_activate_rolls_back_to_earlier_version(prompt_key):
    async with get_session() as session:
        await set_prompt_core(session, key=prompt_key, content="v1")
        await set_prompt_core(session, key=prompt_key, content="v2")
        await session.commit()
    async with get_session() as session:
        await activate_prompt_version_core(session, prompt_key, 1)
        await session.commit()
    async with get_session() as session:
        assert await get_active_prompt_content(session, prompt_key) == "v1"


@pytest.mark.asyncio
async def test_set_no_activate_keeps_pointer(prompt_key):
    async with get_session() as session:
        await set_prompt_core(session, key=prompt_key, content="v1")
        await session.commit()
    async with get_session() as session:
        await set_prompt_core(session, key=prompt_key, content="v2", activate=False)
        await session.commit()
    async with get_session() as session:
        assert await get_active_prompt_content(session, prompt_key) == "v1"


@pytest.mark.asyncio
async def test_get_missing_key_raises_404():
    async with get_session() as session:
        with pytest.raises(HTTPException) as exc:
            await get_prompt_core(session, "does_not_exist_xyz")
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_activate_missing_version_raises_404(prompt_key):
    async with get_session() as session:
        await set_prompt_core(session, key=prompt_key, content="v1")
        await session.commit()
    async with get_session() as session:
        with pytest.raises(HTTPException) as exc:
            await activate_prompt_version_core(session, prompt_key, 99)
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_prompt_core_resolves_by_id(prompt_key):
    async with get_session() as session:
        ver = await set_prompt_core(session, key=prompt_key, content="c1")
        prompt_id = ver.prompt_id
        await session.commit()
    async with get_session() as session:
        prompt, got = await get_prompt_core(session, prompt_id)
        assert prompt.key == prompt_key and got.content == "c1"


@pytest.mark.asyncio
async def test_get_prompt_usage_counts_blocks(prompt_key):
    async with get_session() as session:
        await set_prompt_core(session, key=prompt_key, content="c")
        session.add(AnalyzerBlockModel(
            analyzer_id=f"tr_{prompt_key}", type="trajectory_summary",
            key_prefix="analyzer/trajectory_summary", llm_client_type="api",
            status=JobStatus.SUCCESS, prompt_key=prompt_key, prompt_version=1,
        ))
        await session.commit()
    try:
        async with get_session() as session:
            usage = await get_prompt_usage_core(session, prompt_key)
            assert usage["total"] == 1
            assert usage["by_version"][0]["version"] == 1
            assert usage["last_used_at"] is not None
    finally:
        async with get_session() as session:
            await session.execute(
                AnalyzerBlockModel.__table__.delete().where(
                    AnalyzerBlockModel.prompt_key == prompt_key
                )
            )
            await session.commit()


@pytest.mark.asyncio
async def test_get_prompt_usage_counts_null_version_blocks(prompt_key):
    async with get_session() as session:
        await set_prompt_core(session, key=prompt_key, content="c")
        session.add(AnalyzerBlockModel(
            analyzer_id=f"tr_{prompt_key}_v", type="trajectory_summary",
            key_prefix="analyzer/trajectory_summary", llm_client_type="api",
            status=JobStatus.SUCCESS, prompt_key=prompt_key, prompt_version=1,
        ))
        session.add(AnalyzerBlockModel(
            analyzer_id=f"tr_{prompt_key}_none", type="trajectory_summary",
            key_prefix="analyzer/trajectory_summary", llm_client_type="api",
            status=JobStatus.SUCCESS, prompt_key=prompt_key, prompt_version=None,
        ))
        await session.commit()
    try:
        async with get_session() as session:
            usage = await get_prompt_usage_core(session, prompt_key)
            assert usage["total"] == 2
            versions = {b["version"]: b["count"] for b in usage["by_version"]}
            assert versions == {1: 1, None: 1}
    finally:
        async with get_session() as session:
            await session.execute(
                AnalyzerBlockModel.__table__.delete().where(
                    AnalyzerBlockModel.prompt_key == prompt_key
                )
            )
            await session.commit()


@pytest.mark.asyncio
async def test_get_prompt_usage_zero_for_unused(prompt_key):
    async with get_session() as session:
        await set_prompt_core(session, key=prompt_key, content="c")
        await session.commit()
    async with get_session() as session:
        usage = await get_prompt_usage_core(session, prompt_key)
        assert usage == {"total": 0, "last_used_at": None, "by_version": []}


@pytest.mark.asyncio
async def test_set_prompt_core_appends_by_id(prompt_key):
    async with get_session() as session:
        ver = await set_prompt_core(session, key=prompt_key, content="c1")
        prompt_id = ver.prompt_id
        await session.commit()
    async with get_session() as session:
        v2 = await set_prompt_core(session, key=prompt_id, content="c2")
        await session.commit()
        assert v2.version == 2  # appended to prompt_key, did NOT create a prompt keyed by the id
