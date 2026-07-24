"""Seed content for the built-in analyzer prompts. Idempotent: only creates a
key when it is absent, so operator edits made via the registry are never
clobbered."""

from __future__ import annotations

import hashlib
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
        _load("pre_trial_qa.v2.txt"),
    ),
    PromptKind.QA_POST_TRIAL.value: (
        "Post-trial QA log analysis: classify a trial outcome from its task, trajectory, and verifier artifacts.",
        _load("../classify_prompt.txt"),
    ),
    PromptKind.TRAJECTORY_SUMMARY.value: (
        "Trajectory summary: 2-3 sentence run summary, highlights, taxonomy components.",
        _load("trajectory_summary.v1.txt"),
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

# Append the new built-in prompt as an immutable registry version only when the
# current global content is a byte-for-byte prompt we previously shipped.
# Operator-authored content is deliberately absent from this allowlist.
_KNOWN_PREVIOUS_PROMPT_HASHES: dict[str, set[str]] = {
    PromptKind.QA_PRE_TRIAL.value: {
        "7f97403dd933e42437eb10b61520527b9adadaf81b38e90d28216eeef93ce851",
    },
    PromptKind.QA_POST_TRIAL.value: {
        "6a2b88edea8d27959608c3bb68742e0906c7154f6bd8abcc8828f2df5470edf7",
    },
}


async def seed_prompts(session: AsyncSession) -> list[str]:
    created: list[str] = []
    for kind, (description, content) in PROMPT_SEEDS.items():
        existing = await session.execute(
            select(PromptModel.id).where(
                PromptModel.kind == kind,
                PromptModel.scope_type.is_(None),
                PromptModel.scope_id.is_(None),
            )
        )
        if existing.first() is not None:
            continue
        await set_prompt_core(
            session, kind=kind, content=content, description=description
        )
        created.append(kind)

    for kind, previous_hashes in _KNOWN_PREVIOUS_PROMPT_HASHES.items():
        if kind in created:
            continue
        _, latest = await get_prompt_core(session, kind)
        digest = hashlib.sha256(latest.content.encode("utf-8")).hexdigest()
        is_stub = (
            kind == PromptKind.QA_POST_TRIAL.value
            and latest.content.lstrip().startswith(_LEGACY_POST_TRIAL_STUB_OPENING)
        )
        if digest in previous_hashes or is_stub:
            description, content = PROMPT_SEEDS[kind]
            await set_prompt_core(
                session, kind=kind, content=content, description=description
            )
            suffix = "stub upgraded" if is_stub else "built-in upgraded"
            created.append(f"{kind} ({suffix})")
    return created
