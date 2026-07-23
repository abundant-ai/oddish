"""``configure_storage_paths`` is the async coroutine every worker container
runs before picking up a job (``poll_queue`` / ``reconcile_queue_state`` /
``process_single_job`` all call it first), so it doubles as this codebase's
worker startup hook. Pre-trial QA needs ``QA_PRE_TRIAL``/``QA_POST_TRIAL``
seeded before any QA job runs or ``get_prompt_core`` 404s and every task
records pre_trial FAILED (see ``oddish.core.prompt_seeds``)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

import worker.runtime as runtime
from oddish.core.prompt_seeds import PROMPT_SEEDS
from oddish.db import PromptModel, get_session


async def _delete_seed_prompts() -> None:
    async with get_session() as session:
        for kind in PROMPT_SEEDS:
            await session.execute(
                PromptModel.__table__.delete().where(PromptModel.kind == kind)
            )
        await session.commit()


async def _seeded_kinds() -> set[str]:
    async with get_session() as session:
        result = await session.execute(
            select(PromptModel.kind).where(PromptModel.kind.in_(PROMPT_SEEDS))
        )
        return set(result.scalars().all())


@pytest.mark.asyncio
async def test_seed_analyzer_prompts_creates_missing_keys():
    await _delete_seed_prompts()

    await runtime._seed_analyzer_prompts()

    assert await _seeded_kinds() == set(PROMPT_SEEDS)


@pytest.mark.asyncio
async def test_seed_analyzer_prompts_is_idempotent_on_repeat_calls():
    # First call seeds (or confirms already-seeded); the second must not
    # error or create a duplicate version.
    await runtime._seed_analyzer_prompts()
    await runtime._seed_analyzer_prompts()

    async with get_session() as session:
        for kind in PROMPT_SEEDS:
            prompt = (
                await session.execute(
                    select(PromptModel).where(PromptModel.kind == kind)
                )
            ).scalar_one()
            versions = await prompt.awaitable_attrs.versions
            assert len(versions) == 1


@pytest.mark.asyncio
async def test_seed_analyzer_prompts_swallows_errors(monkeypatch):
    async def _boom(_session):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(runtime, "seed_prompts", _boom)

    # Must not raise: a seeding hiccup can never block the worker from
    # picking up its actual job.
    await runtime._seed_analyzer_prompts()
