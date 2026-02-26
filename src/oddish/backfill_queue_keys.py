from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import settings
from oddish.db import get_session


_KEY_TABLES: tuple[tuple[str, str], ...] = (
    ("trials", "queue_key"),
    ("pgqueuer", "entrypoint"),
    ("pgqueuer_log", "entrypoint"),
)

_MODEL_ABSENT_ALIASES: tuple[str, ...] = ("", "-", "none", "null", "nil", "n/a", "na")


async def _table_exists(session: AsyncSession, table_name: str) -> bool:
    row = (
        await session.execute(text("SELECT to_regclass(:table_name)"), {"table_name": table_name})
    ).scalar_one()
    return row is not None


async def _load_distinct_keys(
    session: AsyncSession, table_name: str, column_name: str
) -> set[str]:
    if not await _table_exists(session, table_name):
        return set()
    rows = await session.execute(
        text(
            f"""
            SELECT DISTINCT {column_name}
            FROM {table_name}
            WHERE {column_name} IS NOT NULL
            """
        )
    )
    return {str(row[0]) for row in rows.fetchall() if row[0]}


def _build_mapping(keys: Iterable[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for old_key in keys:
        new_key = settings.normalize_queue_key(old_key)
        if new_key != old_key:
            mapping[old_key] = new_key
    return mapping


async def _count_rows(
    session: AsyncSession, table_name: str, column_name: str, value: str
) -> int:
    if not await _table_exists(session, table_name):
        return 0
    count = (
        await session.execute(
            text(
                f"""
                SELECT COUNT(*)
                FROM {table_name}
                WHERE {column_name} = :value
                """
            ),
            {"value": value},
        )
    ).scalar_one()
    return int(count)


async def _apply_simple_updates(
    session: AsyncSession, mapping: dict[str, str]
) -> dict[str, int]:
    updated_by_table: dict[str, int] = {}
    for table_name, column_name in _KEY_TABLES:
        if not await _table_exists(session, table_name):
            continue
        updated = 0
        for old_key, new_key in mapping.items():
            if old_key == new_key:
                continue
            result = await session.execute(
                text(
                    f"""
                    UPDATE {table_name}
                    SET {column_name} = :new_key
                    WHERE {column_name} = :old_key
                    """
                ),
                {"old_key": old_key, "new_key": new_key},
            )
            updated += max(int(result.rowcount or 0), 0)
        updated_by_table[table_name] = updated
    return updated_by_table


async def _apply_queue_slot_updates(
    session: AsyncSession, mapping: dict[str, str]
) -> dict[str, int]:
    if not await _table_exists(session, "queue_slots"):
        return {"queue_slots_inserted": 0, "queue_slots_deleted": 0}

    inserted_total = 0
    deleted_total = 0
    for old_key, new_key in mapping.items():
        if old_key == new_key:
            continue

        # Move rows to canonical key; conflicts are ignored because those slots
        # already exist at the canonical key.
        inserted = await session.execute(
            text(
                """
                INSERT INTO queue_slots (queue_key, slot, locked_by, locked_until)
                SELECT :new_key, slot, locked_by, locked_until
                FROM queue_slots
                WHERE queue_key = :old_key
                ON CONFLICT (queue_key, slot) DO NOTHING
                """
            ),
            {"old_key": old_key, "new_key": new_key},
        )
        deleted = await session.execute(
            text("DELETE FROM queue_slots WHERE queue_key = :old_key"),
            {"old_key": old_key},
        )
        inserted_total += max(int(inserted.rowcount or 0), 0)
        deleted_total += max(int(deleted.rowcount or 0), 0)

    return {
        "queue_slots_inserted": inserted_total,
        "queue_slots_deleted": deleted_total,
    }


async def _count_nop_oracle_default_model_updates(session: AsyncSession) -> int:
    if not await _table_exists(session, "trials"):
        return 0
    row = await session.execute(
        text(
            """
            SELECT COUNT(*)
            FROM trials
            WHERE LOWER(COALESCE(agent, '')) IN ('nop', 'oracle')
              AND (
                model IS NULL
                OR LOWER(BTRIM(model)) = ANY(:aliases)
              )
            """
        ),
        {"aliases": list(_MODEL_ABSENT_ALIASES)},
    )
    return int(row.scalar_one())


async def _apply_nop_oracle_default_model_updates(session: AsyncSession) -> int:
    if not await _table_exists(session, "trials"):
        return 0
    result = await session.execute(
        text(
            """
            UPDATE trials
            SET model = 'default'
            WHERE LOWER(COALESCE(agent, '')) IN ('nop', 'oracle')
              AND (
                model IS NULL
                OR LOWER(BTRIM(model)) = ANY(:aliases)
              )
            """
        ),
        {"aliases": list(_MODEL_ABSENT_ALIASES)},
    )
    return int(result.rowcount or 0)


async def run_backfill(*, apply: bool) -> None:
    async with get_session() as session:
        all_keys: set[str] = set()
        for table_name, column_name in _KEY_TABLES:
            all_keys.update(await _load_distinct_keys(session, table_name, column_name))
        all_keys.update(await _load_distinct_keys(session, "queue_slots", "queue_key"))

        mapping = _build_mapping(all_keys)
        model_updates = await _count_nop_oracle_default_model_updates(session)
        if not mapping and model_updates == 0:
            print("No queue keys or nop/oracle model values need canonicalization.")
            return

        if mapping:
            print(f"Found {len(mapping)} non-canonical queue keys:")
            for old_key, new_key in sorted(mapping.items()):
                print(f"  {old_key!r} -> {new_key!r}")
        else:
            print("No non-canonical queue keys found.")

        print("\nAffected row counts (current state):")
        if mapping:
            for table_name, column_name in _KEY_TABLES:
                if not await _table_exists(session, table_name):
                    continue
                table_total = sum(
                    [
                        await _count_rows(session, table_name, column_name, old_key)
                        for old_key in mapping
                    ]
                )
                print(f"  {table_name}: {table_total}")
            if await _table_exists(session, "queue_slots"):
                queue_slots_total = sum(
                    [
                        await _count_rows(session, "queue_slots", "queue_key", old_key)
                        for old_key in mapping
                    ]
                )
                print(f"  queue_slots: {queue_slots_total}")
        print(f"  trials (nop/oracle model->'default'): {model_updates}")

        if not apply:
            print("\nDry run complete. Re-run with --apply to execute updates.")
            return

        simple = await _apply_simple_updates(session, mapping) if mapping else {}
        slots = (
            await _apply_queue_slot_updates(session, mapping)
            if mapping
            else {"queue_slots_inserted": 0, "queue_slots_deleted": 0}
        )
        model_rows = await _apply_nop_oracle_default_model_updates(session)

        print("\nBackfill applied:")
        if simple:
            for table_name, updated in sorted(simple.items()):
                print(f"  {table_name} updated rows: {updated}")
            print(f"  queue_slots inserted rows: {slots['queue_slots_inserted']}")
            print(f"  queue_slots deleted rows: {slots['queue_slots_deleted']}")
        print(f"  trials model updated rows: {model_rows}")
        print("\nDone.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Canonicalize historical queue keys to provider/model form."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute updates. Without this flag, runs in dry-run mode.",
    )
    args = parser.parse_args()
    asyncio.run(run_backfill(apply=args.apply))


if __name__ == "__main__":
    main()
