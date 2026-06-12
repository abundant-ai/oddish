"""Idempotent populate script for probe_presets.

Re-seeds the known global seed presets and inserts org-scoped presets for
a specific org. Safe to run repeatedly: each row is upserted on its
``(org_id, name)`` natural key (matching the partial unique index
``idx_probe_presets_unique_org_name``), so a second run updates in place
rather than duplicating.

Run against the *target* database (point DATABASE_URL at it first):

    python -m oddish.scripts.seed_probe_presets            # dry-run: prints plan
    python -m oddish.scripts.seed_probe_presets --apply    # actually writes

ALWAYS dry-run, eyeball the counts, run against a preview branch, THEN prod.
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from oddish.db import ProbePresetModel, get_session
from oddish.db.connection import AsyncSession

ORG_ID = "9fc60ffe"

# Existing global seed presets (org_id=None, is_seed=True). Paste the rows
# dumped from your source DB here — see the dump command in chat.
SEED_PRESETS: list[dict] = [
    {
        "id": "general-probe",  # fixed PK consumed by auto_probe.GENERAL_PROBE_PRESET_ID
        "name": "General Probe",
        # TODO(human): fill in the real probe content before running --apply.
        # These are the agent slug, model, and the operator directive prepended
        # to instruction.md for every auto-probe. Do NOT ship the placeholders.
        "agent": "TODO_FILL_AGENT_SLUG",
        "model": "TODO_FILL_MODEL",
        "operator_prompt": "TODO_FILL_OPERATOR_PROMPT",
    },
]

# Sentinel prefix marking scaffold values that must be filled before --apply.
_PLACEHOLDER_PREFIX = "TODO_FILL"

# TODO(human): define the 4 new presets owned by org 9fc60ffe.
# Each is org-scoped and mutable (is_seed=False, org_id=ORG_ID is set by
# upsert_preset below — do NOT set org_id here). Required fields: name,
# agent, model, operator_prompt. Optional: result_focus, evaluation_metric,
# ratio_unit, ratio_verb. Names must be unique within the org.
ORG_PRESETS: list[dict] = [
    # {"name": "...", "agent": "...", "model": "...", "operator_prompt": "..."},
]


async def upsert_preset(
    session: AsyncSession,
    *,
    fields: dict,
    org_id: str | None,
    is_seed: bool,
    apply: bool,
) -> str:
    """Insert the preset if (org_id, name) is absent, else update it in place.

    Returns one of "insert" / "update" / "skip" for the run summary. The
    soft-delete session listener auto-scopes this select to deleted_at IS NULL,
    so a tombstoned row of the same name won't block a fresh insert.
    """
    if apply:
        # Fail loudly before any read/write so the scaffold can never reach
        # the DB. Dry-run skips this so the human can still see the plan.
        placeholders = [
            f"{k}={v!r}"
            for k, v in fields.items()
            if isinstance(v, str) and v.startswith(_PLACEHOLDER_PREFIX)
        ]
        if placeholders:
            raise ValueError(
                f"Refusing to seed {fields.get('name')!r}: fill in the TODO "
                f"placeholder content first ({', '.join(placeholders)})."
            )

    existing = (
        await session.execute(
            select(ProbePresetModel).where(
                ProbePresetModel.org_id.is_(org_id) if org_id is None
                else ProbePresetModel.org_id == org_id,
                ProbePresetModel.name == fields["name"],
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        if apply:
            session.add(
                ProbePresetModel(org_id=org_id, is_seed=is_seed, **fields)
            )
        return "insert"

    if apply:
        for key, value in fields.items():
            # Never reassign an existing row's primary key: the row is matched
            # on its (org_id, name) natural key, and id is identity, not data.
            if key == "id":
                continue
            setattr(existing, key, value)
    return "update"


async def main(apply: bool) -> None:
    counts = {"insert": 0, "update": 0, "skip": 0}
    async with get_session() as session:
        for fields in SEED_PRESETS:
            action = await upsert_preset(
                session, fields=fields, org_id=None, is_seed=True, apply=apply
            )
            counts[action] += 1
        for fields in ORG_PRESETS:
            action = await upsert_preset(
                session, fields=fields, org_id=ORG_ID, is_seed=False, apply=apply
            )
            counts[action] += 1
        if not apply:
            # roll back the no-op reads; nothing was written in dry-run anyway
            await session.rollback()

    mode = "APPLIED" if apply else "DRY-RUN (no writes)"
    print(
        f"{mode}: seeds={len(SEED_PRESETS)} org[{ORG_ID}]={len(ORG_PRESETS)} "
        f"-> inserts={counts['insert']} updates={counts['update']}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write to the DB")
    args = parser.parse_args()
    asyncio.run(main(args.apply))
