import pytest

from oddish.core.prompt_seeds import PROMPT_SEEDS, seed_prompts
from oddish.core.prompts import get_latest_prompt_content
from oddish.db import PromptKind, PromptModel, get_session


@pytest.mark.asyncio
async def test_seed_is_idempotent_and_populates_content():
    # clean slate for the seed kinds
    async with get_session() as session:
        for kind in PROMPT_SEEDS:
            await session.execute(PromptModel.__table__.delete().where(PromptModel.kind == kind))
        await session.commit()

    async with get_session() as session:
        created = await seed_prompts(session)
        await session.commit()
        assert set(created) == set(PROMPT_SEEDS)

    async with get_session() as session:
        # second run creates nothing
        created2 = await seed_prompts(session)
        await session.commit()
        assert created2 == []

    async with get_session() as session:
        content = await get_latest_prompt_content(session, PromptKind.QA_PRE_TRIAL.value)
        assert "VERIFIER COMPLETENESS" in content
