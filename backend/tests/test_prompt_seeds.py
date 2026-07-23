import pytest

from oddish.core.prompt_seeds import PROMPT_SEEDS, seed_prompts
from oddish.core.prompts import get_latest_prompt_content
from oddish.db import PromptKind, PromptModel, get_session


@pytest.mark.asyncio
async def test_seed_is_idempotent_and_populates_content():
    # clean slate for the seed kinds
    async with get_session() as session:
        for kind in PROMPT_SEEDS:
            await session.execute(
                PromptModel.__table__.delete().where(PromptModel.kind == kind)
            )
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
        content = await get_latest_prompt_content(
            session, PromptKind.QA_PRE_TRIAL.value
        )
        assert "VERIFIER COMPLETENESS" in content

        post_trial = await get_latest_prompt_content(
            session, PromptKind.QA_POST_TRIAL.value
        )
        from oddish.analyze.classifier import _CLASSIFY_PROMPT

        assert post_trial == _CLASSIFY_PROMPT


@pytest.mark.asyncio
async def test_seed_upgrades_legacy_post_trial_stub_but_not_operator_edits():
    from oddish.analyze.classifier import _CLASSIFY_PROMPT
    from oddish.core.prompts import set_prompt_core

    stub = (
        "You are auditing a single trial trajectory of a Harbor task. You are"
        " given the task's pre-trial action items.\nReturn only the structured"
        " list of action items."
    )

    async with get_session() as session:
        for kind in PROMPT_SEEDS:
            await session.execute(
                PromptModel.__table__.delete().where(PromptModel.kind == kind)
            )
        await session.commit()

    # A DB seeded while the linkage-only stub shipped: heal to the full prompt.
    async with get_session() as session:
        await set_prompt_core(
            session, kind=PromptKind.QA_POST_TRIAL.value, content=stub
        )
        await session.commit()

    async with get_session() as session:
        created = await seed_prompts(session)
        await session.commit()
        assert f"{PromptKind.QA_POST_TRIAL.value} (stub upgraded)" in created

    async with get_session() as session:
        content = await get_latest_prompt_content(
            session, PromptKind.QA_POST_TRIAL.value
        )
        assert content == _CLASSIFY_PROMPT

    # An operator-edited prompt is left alone on subsequent seeds.
    async with get_session() as session:
        await set_prompt_core(
            session, kind=PromptKind.QA_POST_TRIAL.value, content="my custom prompt"
        )
        await session.commit()

    async with get_session() as session:
        created = await seed_prompts(session)
        await session.commit()
        assert created == []

    async with get_session() as session:
        content = await get_latest_prompt_content(
            session, PromptKind.QA_POST_TRIAL.value
        )
        assert content == "my custom prompt"
