"""Regression test: the OSS oddish server boots standalone.

The standalone server (``python -m oddish.server`` / ``oddish.server:api``)
calls ``init_db()`` in its lifespan, which runs
``Base.metadata.create_all``. That only imports the oddish *core* models --
it does NOT import the backend layer (``backend/models.py``), where the
``users`` and ``organizations`` tables live.

A previous regression (PR #414) moved the ``api_keys`` model into core with
hard ``ForeignKey``s to ``users.id`` / ``organizations.id``. Because those
tables are backend-only and never registered on the shared ``Base.metadata``
in an OSS process, ``create_all``'s table sort raised
``NoReferencedTableError`` and the server crashed on startup against a fresh
DB.

This had to be reproduced in a *subprocess*: the rest of this test suite
imports ``backend.models`` (which registers ``users``/``organizations`` on
the module-global ``Base.metadata``), so within a single interpreter the bug
is masked. A cold interpreter that imports only oddish core is the faithful
reproduction.

``create_mock_engine`` compiles the DDL with the real PostgreSQL dialect
without needing a live database, so this runs anywhere ``pytest`` does.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"

# Runs in a fresh interpreter that imports ONLY oddish core -- mirrors what an
# OSS user gets from `python -m oddish.server` against a fresh database. If any
# core table declares a FK to a backend-only table, `create_all`'s table sort
# raises NoReferencedTableError here and the process exits non-zero.
_CHILD = """
from sqlalchemy import create_mock_engine
from sqlalchemy.dialects import postgresql

# Import ONLY oddish core. Importing backend.models here would register
# users/organizations on the shared Base.metadata and mask the bug.
from oddish.db.models import Base

assert "users" not in Base.metadata.tables, "users leaked into core metadata"
assert "organizations" not in Base.metadata.tables, "organizations leaked into core metadata"
assert "api_keys" in Base.metadata.tables, "api_keys missing from core metadata"

# Compile each DDL element with the real PostgreSQL dialect (handles ENUM/JSONB);
# the executor never touches a live DB.
_pg = postgresql.dialect()
emitted = []
engine = create_mock_engine(
    "postgresql://", lambda sql, *a, **k: emitted.append(str(sql.compile(dialect=_pg)))
)

# This is exactly what init_db() does in the server lifespan. With a core->backend
# FK present it raises sqlalchemy.exc.NoReferencedTableError before any DDL runs.
Base.metadata.create_all(engine, checkfirst=False)

ddl = "\\n".join(emitted)
assert "CREATE TABLE api_keys" in ddl, "api_keys CREATE TABLE was not emitted"
print("OK")
"""


def test_oss_server_metadata_create_all_standalone() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _CHILD],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(_SRC), "PATH": __import__("os").environ.get("PATH", "")},
    )
    assert result.returncode == 0, (
        "OSS core failed Base.metadata.create_all standalone -- a core table "
        "likely has a cross-layer FK to a backend-only table.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout, result.stdout
