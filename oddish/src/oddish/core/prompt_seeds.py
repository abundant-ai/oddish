"""Seed content for the built-in analyzer prompts. Idempotent: only creates a
key when it is absent, so operator edits made via the registry are never
clobbered."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.prompts import get_prompt_core, set_prompt_core
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


# The original QA_POST_TRIAL seed: a linkage-only stub that was never consumed
# at runtime (the classifier inlined classify_prompt.txt). DBs seeded while it
# shipped hold it as their latest version; self-heal by appending the full
# classify prompt, since running post-trial QA with only the stub would drop
# the entire log-analysis instruction set. Latest-wins makes the append safe,
# and operator edits (any other content) are never clobbered.
_LEGACY_POST_TRIAL_STUB_OPENING = (
    "You are auditing a single trial trajectory of a Harbor task."
)


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

    post_trial_kind = PromptKind.QA_POST_TRIAL.value
    if post_trial_kind not in created:
        _, latest = await get_prompt_core(session, post_trial_kind)
        if latest.content.lstrip().startswith(_LEGACY_POST_TRIAL_STUB_OPENING):
            description, content = PROMPT_SEEDS[post_trial_kind]
            await set_prompt_core(
                session, kind=post_trial_kind, content=content, description=description
            )
            created.append(f"{post_trial_kind} (stub upgraded)")
    return created
