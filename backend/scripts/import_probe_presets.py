"""One-shot importer for legacy probe-presets.json into the probe_presets table.

Run once after applying the b5c6d7e8f9a0_add_probe_presets migration to backfill
any custom presets that were stored in the old gitignored flat file. Idempotent
on (org_id, id): re-running will skip rows that already exist.

Usage::

    ODDISH_DATABASE_URL=postgresql+asyncpg://user:pw@host/db \\
        uv run python backend/scripts/import_probe_presets.py \\
        --org-id ORG_ID \\
        [--json /path/to/probe-presets.json]   # default: ./probe-presets.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from oddish.db import ProbePreset, get_session, utcnow


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _import(json_path: Path, org_id: str) -> None:
    raw = json.loads(json_path.read_text())
    if not isinstance(raw, list):
        sys.exit(f"{json_path}: expected a JSON array, got {type(raw).__name__}")

    inserted = 0
    skipped = 0
    now = utcnow()

    async with get_session() as session:
        for entry in raw:
            preset_id = entry.get("id")
            if not preset_id:
                continue
            existing = (
                await session.execute(
                    select(ProbePreset).where(
                        ProbePreset.id == preset_id,
                        ProbePreset.org_id == org_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                skipped += 1
                continue
            session.add(
                ProbePreset(
                    id=preset_id,
                    org_id=org_id,
                    name=entry.get("name", preset_id),
                    agent=entry.get("agent", "claude-code"),
                    model=entry.get("model", "anthropic/claude-sonnet-4-6"),
                    operator_prompt=entry.get("operator_prompt", ""),
                    result_focus=entry.get("result_focus"),
                    evaluation_metric=entry.get("evaluation_metric"),
                    ratio_unit=entry.get("ratio_unit"),
                    ratio_verb=entry.get("ratio_verb"),
                    include_prior_attempts=entry.get("include_prior_attempts"),
                    is_seed=bool(entry.get("is_seed", False)),
                    created_at=_parse_dt(entry.get("created_at")) or now,
                    updated_at=_parse_dt(entry.get("updated_at")) or now,
                )
            )
            inserted += 1
        await session.commit()

    print(f"imported {inserted} preset(s) for org={org_id}; skipped {skipped} existing")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org-id", required=True, help="org_id to attribute the imported presets to")
    parser.add_argument(
        "--json",
        type=Path,
        default=Path("probe-presets.json"),
        help="path to the legacy probe-presets.json (default: ./probe-presets.json)",
    )
    args = parser.parse_args()
    if not args.json.is_file():
        sys.exit(f"{args.json}: file not found")
    asyncio.run(_import(args.json, args.org_id))


if __name__ == "__main__":
    main()
