import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException

from oddish.core.prompts import (
    get_latest_prompt_content,
    get_prompt_core,
    get_prompt_usage_core,
    list_prompt_versions_core,
    set_prompt_core,
)
from oddish.db import JobStatus, PromptModel, get_session
from oddish.db.models import AnalyzerBlockModel


@pytest_asyncio.fixture
async def prompt_kind():
    # Random throwaway kind: core is deliberately string-typed (enum is
    # enforced at the router), so tests never collide with the seeded rows.
    kind = f"test_prompt_{uuid.uuid4().hex[:8]}"
    yield kind
    async with get_session() as session:
        await session.execute(
            PromptModel.__table__.delete().where(PromptModel.kind == kind)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_set_creates_v1(prompt_kind):
    async with get_session() as session:
        v = await set_prompt_core(
            session, kind=prompt_kind, content="hello", description="d"
        )
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


@pytest.mark.asyncio
async def test_get_prompt_core_resolves_by_id(prompt_kind):
    async with get_session() as session:
        ver = await set_prompt_core(session, kind=prompt_kind, content="c1")
        prompt_id = ver.prompt_id
        await session.commit()
    async with get_session() as session:
        prompt, got = await get_prompt_core(session, prompt_id)
        assert prompt.kind == prompt_kind and got.content == "c1"


@pytest.mark.asyncio
async def test_get_prompt_usage_counts_blocks(prompt_kind):
    async with get_session() as session:
        await set_prompt_core(session, kind=prompt_kind, content="c")
        session.add(
            AnalyzerBlockModel(
                analyzer_id=f"tr_{prompt_kind}",
                type="trajectory_summary",
                key_prefix="analyzer/trajectory_summary",
                llm_client_type="api",
                status=JobStatus.SUCCESS,
                prompt_key=prompt_kind,
                prompt_version=1,
            )
        )
        await session.commit()
    try:
        async with get_session() as session:
            usage = await get_prompt_usage_core(session, prompt_kind)
            assert usage["total"] == 1
            assert usage["by_version"][0]["version"] == 1
            assert usage["last_used_at"] is not None
    finally:
        async with get_session() as session:
            await session.execute(
                AnalyzerBlockModel.__table__.delete().where(
                    AnalyzerBlockModel.prompt_key == prompt_kind
                )
            )
            await session.commit()


@pytest.mark.asyncio
async def test_get_prompt_usage_counts_null_version_blocks(prompt_kind):
    async with get_session() as session:
        await set_prompt_core(session, kind=prompt_kind, content="c")
        session.add(
            AnalyzerBlockModel(
                analyzer_id=f"tr_{prompt_kind}_v",
                type="trajectory_summary",
                key_prefix="analyzer/trajectory_summary",
                llm_client_type="api",
                status=JobStatus.SUCCESS,
                prompt_key=prompt_kind,
                prompt_version=1,
            )
        )
        session.add(
            AnalyzerBlockModel(
                analyzer_id=f"tr_{prompt_kind}_none",
                type="trajectory_summary",
                key_prefix="analyzer/trajectory_summary",
                llm_client_type="api",
                status=JobStatus.SUCCESS,
                prompt_key=prompt_kind,
                prompt_version=None,
            )
        )
        await session.commit()
    try:
        async with get_session() as session:
            usage = await get_prompt_usage_core(session, prompt_kind)
            assert usage["total"] == 2
            versions = {b["version"]: b["count"] for b in usage["by_version"]}
            assert versions == {1: 1, None: 1}
    finally:
        async with get_session() as session:
            await session.execute(
                AnalyzerBlockModel.__table__.delete().where(
                    AnalyzerBlockModel.prompt_key == prompt_kind
                )
            )
            await session.commit()


@pytest.mark.asyncio
async def test_get_prompt_usage_zero_for_unused(prompt_kind):
    async with get_session() as session:
        await set_prompt_core(session, kind=prompt_kind, content="c")
        await session.commit()
    async with get_session() as session:
        usage = await get_prompt_usage_core(session, prompt_kind)
        assert usage == {"total": 0, "last_used_at": None, "by_version": []}


@pytest.mark.asyncio
async def test_set_prompt_core_appends_by_id(prompt_kind):
    async with get_session() as session:
        ver = await set_prompt_core(session, kind=prompt_kind, content="c1")
        prompt_id = ver.prompt_id
        await session.commit()
    async with get_session() as session:
        v2 = await set_prompt_core(session, kind=prompt_id, content="c2")
        await session.commit()
        assert v2.version == 2  # appended to prompt_kind; did not create an id-kind
