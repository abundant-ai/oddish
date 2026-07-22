"""Seed content for the built-in analyzer prompts. Idempotent: only creates a
key when it is absent, so operator edits made via the registry are never
clobbered."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.prompts import set_prompt_core
from oddish.db import PromptModel

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "analyze" / "prompts"


def _load(name: str) -> str:
    return (_PROMPT_DIR / name).read_text()


PROMPT_SEEDS: dict[str, tuple[str, str]] = {
    "pre_trial_qa": (
        "Pre-trial QA auditor: verifier completeness, oracle correctness, info leakage.",
        _load("pre_trial_qa.v1.txt"),
    ),
    # NOTE: seeded for the registry, but not currently wired up -- the live
    # classifier (analyze/classifier.py) sends the post-trial instructions
    # inlined in analyze/classify_prompt.txt, not this registry entry.
    # Editing this key's content via the registry has no runtime effect
    # until the classifier is switched to fetch from the registry.
    "post_trial_qa": (
        "Post-trial QA: exploited/causal assessment + new trajectory action items.",
        _load("post_trial_qa.v1.txt"),
    ),
}


async def seed_prompts(session: AsyncSession) -> list[str]:
    created: list[str] = []
    for key, (description, content) in PROMPT_SEEDS.items():
        existing = await session.execute(
            select(PromptModel.id).where(PromptModel.key == key)
        )
        if existing.scalar_one_or_none() is not None:
            continue
        await set_prompt_core(
            session, key=key, content=content, description=description
        )
        created.append(key)
    return created
