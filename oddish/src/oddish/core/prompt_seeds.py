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
        _load("pre_trial_qa.txt"),
    ),
    PromptKind.QA_POST_TRIAL.value: (
        "Post-trial QA log analysis: classify a trial outcome from its task, trajectory, and verifier artifacts.",
        _load("../classify_prompt.txt"),
    ),
    PromptKind.TRAJECTORY_SUMMARY.value: (
        "Trajectory summary: 2-3 sentence run summary, highlights, taxonomy components.",
        _load("trajectory_summary.txt"),
    ),
}


# Seeder-written versions carry this sentinel in ``created_by`` (operator
# edits carry a real user id). It lets a later seed run distinguish a shipped
# built-in from an operator edit without tracking hashes: a built-in is
# safe to upgrade, an operator edit is never touched.
BUILTIN_CREATED_BY = "__builtin__"

# The original QA_POST_TRIAL seed: a linkage-only stub that was never consumed
# at runtime (the classifier inlined classify_prompt.txt). DBs seeded while it
# shipped hold it as their latest version; self-heal by appending the full
# classify prompt, since running post-trial QA with only the stub would drop
# the entire log-analysis instruction set. Latest-wins makes the append safe,
# and operator edits (any other content) are never clobbered.
_LEGACY_POST_TRIAL_STUB_OPENING = (
    "You are auditing a single trial trajectory of a Harbor task."
)

# LEGACY ONLY. Databases seeded before BUILTIN_CREATED_BY existed hold
# built-in versions with created_by=NULL, which does not show who wrote them --
# for those, a byte-for-byte hash of a previously shipped prompt is the only
# proof the content is ours to upgrade. Versions seeded from now on carry the
# sentinel instead, so NEW prompt edits must NOT add hashes here.
# Operator-authored content is deliberately absent from this allowlist.
_KNOWN_PREVIOUS_PROMPT_HASHES: dict[str, set[str]] = {
    PromptKind.QA_PRE_TRIAL.value: {
        # Shipped v1-v4 built-ins: upgrade any of them to the current seed.
        # Operator-edited content matches no hash and is never touched.
        "7f97403dd933e42437eb10b61520527b9adadaf81b38e90d28216eeef93ce851",
        "d76958a450cf046054869030dc27be53551cf3ecd79d30dc19d862e283cff3a4",
        "a305ad1dc1be4f8209534d95d13c021c49560bf73d7ae63901a9d70187955657",
        "c83ddf0a72d3d4632959a0a4819d0b17ca40bf5ae8479bca1c97f806e17f3f4a",
    },
    PromptKind.TRAJECTORY_SUMMARY.value: {
        # Original v1 content, seeded before the created_by sentinel existed:
        # upgrade to the plain-language edit.
        "4b77a6847574474f2f552b7dac61d63951e7907688fea909d16adb531c7f0d23",
    },
    PromptKind.QA_POST_TRIAL.value: {
        "6a2b88edea8d27959608c3bb68742e0906c7154f6bd8abcc8828f2df5470edf7",
        # Classify prompt from before the REPORT STYLE section existed: superseded by the plain-language
        # output rules (REPORT STYLE section, exact-"N/A" recommendation,
        # secondary findings moved to action_items).
        "07834839a317be6b2f1e0a706a60c5dbf7ba88c2d029c7e3d601e99440532e27",
        # Interim report-style version: superseded by the full plain-language
        # rewrite of the whole prompt.
        "7447cc0ba256a6f5dbda27d22adde4a4f8b913cf13a5471498173ca123ae4dbb",
        # Plain-language rewrite that still forced partial rewards into the
        # failure buckets: superseded by the rule that a partial reward may take any label.
        "3c4db2c88bf53befbc7727fe7620ac6c40656a9c66b1c5afef106381a00e92d8",
        # Version that treated hidden-file reads as a harness issue only and never
        # checked privilege escalation: superseded by the rule that checks both
        # cheating and information leaks.
        "04cb0bfa2867554a7e2d0ff6e1b203fa73a85993f2b208566001ee7f070be8b1",
        # Version without the general conciseness rule for action_items and
        # exploitation fields.
        "276ef1117ee000f32d27c875a04a2a44c06ba5669407719928d461e99bd01780",
        # Last version with prompt-internal shorthand (Q1/Q2, "causality
        # rule", "gaming"): superseded by standard terminology (reward
        # hacking) and the seeder sentinel; no more hashes need to be added here.
        "fca45b2fe7cd3ef1cc97260d3fbeca251900f60c745d883bace52825d6625f02",
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
            session,
            kind=kind,
            content=content,
            description=description,
            created_by=BUILTIN_CREATED_BY,
        )
        created.append(kind)

    for kind, (description, content) in PROMPT_SEEDS.items():
        if kind in created:
            continue
        _, latest = await get_prompt_core(session, kind)
        if latest.content == content:
            # Backfill the sentinel whenever the content is byte-for-byte the
            # shipped seed. Content equality is the proof, whoever wrote the
            # row: rows seeded before the sentinel existed carry NULL, and
            # `oddish prompt seed` writes stock content under the operator's
            # user id. Without this stamp, the NEXT content change could not
            # upgrade these rows.
            if latest.created_by != BUILTIN_CREATED_BY:
                latest.created_by = BUILTIN_CREATED_BY
                await session.flush()
            continue
        # Upgrade only content we shipped: sentinel-stamped versions, plus the
        # legacy hash/stub checks for rows seeded before the sentinel existed.
        digest = hashlib.sha256(latest.content.encode("utf-8")).hexdigest()
        is_stub = (
            kind == PromptKind.QA_POST_TRIAL.value
            and latest.content.lstrip().startswith(_LEGACY_POST_TRIAL_STUB_OPENING)
        )
        is_builtin = (
            latest.created_by == BUILTIN_CREATED_BY
            or digest in _KNOWN_PREVIOUS_PROMPT_HASHES.get(kind, set())
        )
        if is_builtin or is_stub:
            await set_prompt_core(
                session,
                kind=kind,
                content=content,
                description=description,
                created_by=BUILTIN_CREATED_BY,
            )
            suffix = "stub upgraded" if is_stub else "built-in upgraded"
            created.append(f"{kind} ({suffix})")
    return created
