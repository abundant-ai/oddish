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
        # The taxonomy has to be taught in the JSON spellings the model must
        # emit. Teaching it as prose headings instead ("SEVERITY", "VERIFIER
        # COMPLETENESS") is what produced items keyed `severity` and
        # dimension="verifier_completeness" -- each one failing a whole audit.
        assert '"source": "pre_trial"' in content
        assert '"dimension": "verifier"' in content
        assert '"tier": "must_fix"' in content
        assert '`"oracle"`' in content
        assert '`"incompleteness"`' in content
        assert '`"should_fix"`' in content

        post_trial = await get_latest_prompt_content(
            session, PromptKind.QA_POST_TRIAL.value
        )
        from oddish.analyze.classifier import _CLASSIFY_PROMPT

        assert post_trial == _CLASSIFY_PROMPT
        assert "exactly\none entry per item" in post_trial
        assert '"action_items": [' in post_trial
        assert '"exploitation": [' in post_trial


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


@pytest.mark.asyncio
async def test_seed_upgrades_known_pre_trial_v1_but_not_operator_edits():
    from oddish.core.prompts import set_prompt_core

    kind = PromptKind.QA_PRE_TRIAL.value
    # Load the retained v1 fixture: production upgrades are keyed to this exact
    # content hash, not a loose prefix that could overwrite an operator edit.
    from pathlib import Path

    v1 = (
        Path(__file__).parents[2]
        / "oddish"
        / "src"
        / "oddish"
        / "analyze"
        / "prompts"
        / "pre_trial_qa.v1.txt"
    ).read_text()

    async with get_session() as session:
        await session.execute(
            PromptModel.__table__.delete().where(PromptModel.kind == kind)
        )
        await set_prompt_core(session, kind=kind, content=v1)
        await session.commit()

    async with get_session() as session:
        created = await seed_prompts(session)
        await session.commit()
        assert f"{kind} (built-in upgraded)" in created

    async with get_session() as session:
        content = await get_latest_prompt_content(session, kind)
        assert content == PROMPT_SEEDS[kind][1]
        assert '"source": "pre_trial"' in content

        await set_prompt_core(session, kind=kind, content="my taxonomy override")
        await session.commit()

    async with get_session() as session:
        assert await seed_prompts(session) == []
        content = await get_latest_prompt_content(session, kind)
        assert content == "my taxonomy override"


@pytest.mark.asyncio
async def test_seed_completes_when_scoped_row_already_exists_for_seeded_kind():
    """A scoped override coexisting with the global row for a seeded kind must
    not break the existence probe. Before the fix, the probe matched by
    ``kind`` alone -- global plus an org override made it return two rows,
    and ``scalar_one_or_none()`` raised ``MultipleResultsFound``, aborting
    seeding for every kind in ``PROMPT_SEEDS``, not just this one."""
    from oddish.core.prompts import set_prompt_core

    kind = PromptKind.QA_PRE_TRIAL.value
    org_id = "org-scoped-seed-guard"

    # Establish the normal global row first (idempotent if already seeded).
    async with get_session() as session:
        await seed_prompts(session)
        await session.commit()

    async with get_session() as session:
        await set_prompt_core(
            session,
            kind=kind,
            content="org override",
            scope_type="org",
            scope_id=org_id,
            org_id=org_id,
        )
        await session.commit()

    try:
        async with get_session() as session:
            created = await seed_prompts(session)
            await session.commit()
        # The global row already existed, so this kind is not re-created --
        # this also proves the probe didn't mistake the org row for it.
        assert kind not in created
    finally:
        async with get_session() as session:
            await session.execute(
                PromptModel.__table__.delete().where(
                    PromptModel.kind == kind,
                    PromptModel.scope_type == "org",
                    PromptModel.scope_id == org_id,
                )
            )
            await session.commit()
