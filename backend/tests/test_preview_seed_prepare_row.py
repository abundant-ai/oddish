"""Pure-function tests for the preview seed's row preparation (no database).

``test_preview_seed.py`` skips entirely without ``ODDISH_DATABASE_URL``; these
checks don't touch Postgres, so they live here and always run. They guard the
dropped-column warning (it must fire at most once per (table, column), or a
six-figure draw floods stderr) and JSON/JSONB coercion.
"""

from sqlalchemy import JSON, Column, MetaData, String, Table
from sqlalchemy.dialects.postgresql import JSONB

import models  # noqa: F401  registers the cloud tables on the shared Base
import preview_seed


def _toy_table() -> Table:
    md = MetaData()
    return Table(
        "worker_jobs",
        md,
        Column("id", String, primary_key=True),
        Column("payload", JSONB),
    )


def test_prepare_row_drops_unknown_columns_warning_once(capsys, monkeypatch):
    """A column present in prod but absent from the branch schema warns ONCE
    across many rows, not once per row -- otherwise a six-figure draw floods
    stderr and the job hangs for hours."""
    monkeypatch.setattr(preview_seed, "_warned_dropped_columns", set())
    table = _toy_table()

    rows = [
        {"id": f"wj-{i}", "payload": {"k": i}, "provider": "anthropic", "external_id": "x"}
        for i in range(50)
    ]
    prepared = [preview_seed._prepare_row(table, r) for r in rows]

    # Behavior preserved: known columns kept, unknown dropped.
    assert all(set(p) == {"id", "payload"} for p in prepared)

    err = capsys.readouterr().err
    assert err.count("dropped worker_jobs.provider") == 1
    assert err.count("dropped worker_jobs.external_id") == 1


def test_prepare_row_warns_per_distinct_column_and_table(capsys, monkeypatch):
    """Each distinct (table, column) still gets its own legible warning."""
    monkeypatch.setattr(preview_seed, "_warned_dropped_columns", set())
    table = _toy_table()

    preview_seed._prepare_row(table, {"id": "a", "provider": "p"})
    preview_seed._prepare_row(table, {"id": "b", "external_id": "e"})

    err = capsys.readouterr().err
    assert err.count("dropped worker_jobs.provider") == 1
    assert err.count("dropped worker_jobs.external_id") == 1


def test_prepare_row_preserves_json_coercion_and_filtering():
    """JSON/JSONB strings are decoded; columns are still filtered to the
    target schema."""
    md = MetaData()
    table = Table(
        "t",
        md,
        Column("id", String, primary_key=True),
        Column("blob", JSON),
    )
    out = preview_seed._prepare_row(
        table, {"id": "x", "blob": '{"a": 1}', "gone": "y"}
    )
    assert out == {"id": "x", "blob": {"a": 1}}
