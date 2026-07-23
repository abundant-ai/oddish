import pytest

from oddish.core.prompt_seeds import PROMPT_SEEDS, seed_prompts
from oddish.core.prompts import get_active_prompt_content
from oddish.db import PromptModel, get_session


@pytest.mark.asyncio
async def test_seed_is_idempotent_and_populates_content():
    # clean slate for the seed keys
    async with get_session() as session:
        for key in PROMPT_SEEDS:
            await session.execute(PromptModel.__table__.delete().where(PromptModel.key == key))
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
        content = await get_active_prompt_content(session, "pre_trial_qa")
        assert "VERIFIER COMPLETENESS" in content


def test_trajectory_summary_seed_registered():
    assert "trajectory_summary" in PROMPT_SEEDS


@pytest.mark.asyncio
async def test_trajectory_summary_seed_creates_key():
    async with get_session() as session:
        await session.execute(
            PromptModel.__table__.delete().where(PromptModel.key == "trajectory_summary")
        )
        await session.commit()

    async with get_session() as session:
        created = await seed_prompts(session)
        await session.commit()
        assert "trajectory_summary" in created

    async with get_session() as session:
        content = await get_active_prompt_content(session, "trajectory_summary")
        assert "{{taxonomy}}" in content
        assert "Highlights must be ordered by step_id ascending." in content
