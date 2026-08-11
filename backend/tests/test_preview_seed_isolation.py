"""Unit tests for the divide-and-conquer seed fallback (no database).

A batch with k constraint-violating rows among n rows must be recovered in
O(k log n) round trips -- not the O(n) row-by-row replay that made #1139's
seed take 318s -- while valid rows all land, invalid rows are reported with
their constraint identity, and transient infrastructure errors abort the load
instead of being misclassified as bad data.
"""

import math

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, OperationalError

import preview_seed


class _PgConstraintError(Exception):
    """Stands in for asyncpg's driver error: carries SQLSTATE + constraint."""

    sqlstate = "23505"
    constraint_name = "tasks_org_name_key"


def _integrity_error() -> IntegrityError:
    return IntegrityError("INSERT ...", {}, _PgConstraintError("duplicate key"))


async def _run_isolation(n: int, bad_ids: set[str]):
    executed: list[list[str]] = []
    loaded: set[str] = set()
    reported: list[tuple[str, str]] = []
    stats = {"batches_attempted": 0, "batches_split": 0, "rows_skipped": 0}

    async def execute(rows):
        ids = [r["id"] for r in rows]
        executed.append(ids)
        if any(i in bad_ids for i in ids):
            raise _integrity_error()
        loaded.update(ids)

    def report_row(row, exc):
        reported.append((row["id"], preview_seed._error_cause(exc)))

    rows = [{"id": f"row-{i:04d}"} for i in range(n)]
    await preview_seed._isolate_bad_rows(execute, rows, report_row, stats)
    return executed, loaded, reported, stats



@pytest.mark.asyncio
async def test_all_rows_valid_loads_in_one_batch():
    executed, loaded, reported, stats = await _run_isolation(1000, set())
    assert len(executed) == 1
    assert len(loaded) == 1000
    assert reported == []
    assert stats == {"batches_attempted": 1, "batches_split": 0, "rows_skipped": 0}



@pytest.mark.asyncio
async def test_one_invalid_row_in_a_thousand():
    executed, loaded, reported, stats = await _run_isolation(1000, {"row-0500"})
    assert [r for r, _ in reported] == ["row-0500"]
    assert loaded == {f"row-{i:04d}" for i in range(1000)} - {"row-0500"}
    # O(k log n): ~2 * log2(1000) probes, not 1001 row-by-row statements.
    assert stats["batches_attempted"] <= 2 * math.ceil(math.log2(1000)) + 1
    assert stats["rows_skipped"] == 1



@pytest.mark.asyncio
async def test_five_invalid_rows_spread_across_a_thousand():
    bad = {f"row-{i:04d}" for i in (3, 200, 500, 777, 999)}
    _, loaded, reported, stats = await _run_isolation(1000, bad)
    assert {r for r, _ in reported} == bad
    assert loaded == {f"row-{i:04d}" for i in range(1000)} - bad
    assert stats["rows_skipped"] == 5
    # Each bad row costs ~2*log2(n) probes and they share upper-level splits.
    assert stats["batches_attempted"] <= 4 * len(bad) * math.ceil(math.log2(1000))



@pytest.mark.asyncio
async def test_all_rows_invalid_reports_every_row():
    n = 64
    bad = {f"row-{i:04d}" for i in range(n)}
    _, loaded, reported, stats = await _run_isolation(n, bad)
    assert {r for r, _ in reported} == bad
    assert loaded == set()
    # A fully-failing tree is exactly n leaves + (n - 1) internal splits.
    assert stats["batches_attempted"] == 2 * n - 1
    assert stats["batches_split"] == n - 1



@pytest.mark.asyncio
async def test_transient_db_error_is_fatal_not_a_skip():
    stats = {"batches_attempted": 0, "batches_split": 0, "rows_skipped": 0}
    reported = []

    async def execute(rows):
        raise OperationalError("INSERT ...", {}, Exception("connection reset"))

    with pytest.raises(OperationalError):
        await preview_seed._isolate_bad_rows(
            execute,
            [{"id": "a"}, {"id": "b"}],
            lambda row, exc: reported.append(row),
            stats,
        )
    assert reported == []
    assert stats["rows_skipped"] == 0



@pytest.mark.asyncio
async def test_reported_cause_and_row_ids_are_deterministic():
    bad = {f"row-{i:04d}" for i in (3, 200, 500, 777, 999)}
    first = await _run_isolation(1000, bad)
    second = await _run_isolation(1000, bad)
    assert first[2] == second[2]  # reported rows, in recursion (row) order
    assert first[3] == second[3]  # counters
    assert [r for r, _ in first[2]] == sorted(bad)
    for _, cause in first[2]:
        assert cause == "_PgConstraintError (SQLSTATE 23505, constraint tasks_org_name_key)"


def test_row_key_formats_composite_primary_keys():
    md = sa.MetaData()
    table = sa.Table(
        "task_experiments",
        md,
        sa.Column("task_id", sa.String, primary_key=True),
        sa.Column("experiment_id", sa.String, primary_key=True),
        sa.Column("added_at", sa.DateTime),
    )
    row = {"task_id": "task-9", "experiment_id": "exp-3", "added_at": None}
    assert preview_seed._row_key(table, row) == "task-9:exp-3"


def test_seed_report_sorts_causes_and_keys_and_caps_keys():
    stats = {"batches_attempted": 9, "batches_split": 4, "rows_skipped": 3}
    skips = {
        "z-cause": ["tasks.b", "tasks.a"],
        "a-cause": [f"tasks.r{i:03d}" for i in range(150)],
    }
    report = preview_seed._seed_report(stats, skips, {"tasks": [10, 12]})
    assert list(report["skips"]) == ["a-cause", "z-cause"]
    assert report["skips"]["z-cause"] == ["tasks.a", "tasks.b"]
    assert len(report["skips"]["a-cause"]) == 100
    assert report["tables"] == {"tasks": [10, 12]}
    assert report["batches_split"] == 4
