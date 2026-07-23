"""Seed content for the built-in analyzer prompts. Idempotent: only creates a
key when it is absent, so operator edits made via the registry are never
clobbered."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.prompts import set_prompt_core
from oddish.db import PromptKind, PromptModel

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "analyze" / "prompts"


def _load(name: str) -> str:
    return (_PROMPT_DIR / name).read_text()


PROMPT_SEEDS: dict[str, tuple[str, str]] = {
    PromptKind.QA_PRE_TRIAL.value: (
        "Pre-trial QA auditor: verifier completeness, oracle correctness, info leakage.",
        _load("pre_trial_qa.v1.txt"),
    ),
    PromptKind.QA_POST_TRIAL.value: (
        "Post-trial QA log analysis: classify a trial outcome from its task, trajectory, and verifier artifacts.",
        _load("../classify_prompt.txt"),
    ),
}


async def seed_prompts(session: AsyncSession) -> list[str]:
    created: list[str] = []
    for kind, (description, content) in PROMPT_SEEDS.items():
        existing = await session.execute(
            select(PromptModel.id).where(PromptModel.kind == kind)
        )
        if existing.scalar_one_or_none() is not None:
            continue
        await set_prompt_core(
            session, kind=kind, content=content, description=description
        )
        created.append(kind)
    return created
