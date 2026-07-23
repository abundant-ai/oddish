#!/usr/bin/env python3
"""CI guard: require ``lock_timeout`` on migrations that lock a hot table.

Why this exists
---------------
``.github/workflows/supabase-db-migrations.yml`` runs ``alembic upgrade head``
against the **production** database on every push to ``main``, while the app is
serving traffic. A migration that takes ``ACCESS EXCLUSIVE`` on a hot table is
therefore competing with live queries, and two things can go wrong:

* **Deadlock.** The migration and the app acquire the same tables in opposite
  orders and Postgres kills one of them. ``prod_schema_repair_001`` (#891) did
  exactly this -- it locked ``task_versions`` (via ``add_column``) and then
  asked for ``tasks``, while live traffic locks ``tasks`` first and then reads
  ``task_versions``. Three pushes to main failed in a row.
* **Stall.** A *pending* ``ACCESS EXCLUSIVE`` request queues every subsequent
  query on that table behind it. Without ``lock_timeout`` a migration blocked
  on one slow transaction takes the table -- and the product -- down with it.

What this guard does
--------------------
For each migration file passed on the command line (CI passes only the ones the
PR adds or modifies), the guard statically finds operations that take a heavy
lock on a table in ``HOT_TABLES`` and fails unless the migration also sets
``lock_timeout``.

Why the existing checks can't catch this
----------------------------------------
``migration-upgrade-check.yml`` replays migrations against a *fresh* database.
Fresh databases have no drifted columns, so guarded branches like
``if column in existing_columns`` never execute and the dangerous DDL is never
reached. That job was green for every one of the three failing prod runs. Nor
is contention simulated anywhere: an uncontended migration never deadlocks.

This guard is deliberately narrow. It asserts the one invariant that is
statically checkable -- a bounded lock wait -- and points at lock *ordering* in
its output, which is a judgement call the author has to make.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

# Tables the live request path reads constantly; an ACCESS EXCLUSIVE lock here
# is a production stall, not just a slow migration.
HOT_TABLES = frozenset({"tasks", "trials", "experiments", "task_versions"})

# alembic operations that take ACCESS EXCLUSIVE (or otherwise write-blocking)
# locks for their whole transaction.
LOCKING_OPS = frozenset(
    {
        "add_column",
        "drop_column",
        "alter_column",
        "drop_table",
        "rename_table",
        "drop_constraint",
        "create_foreign_key",
        "create_check_constraint",
        "create_unique_constraint",
        "create_primary_key",
        "batch_alter_table",
    }
)

_ALTER_TABLE_SQL = re.compile(
    r"\bALTER\s+TABLE\s+(?:ONLY\s+)?\"?(\w+)\"?", re.IGNORECASE
)


class _Finding(ast.NodeVisitor):
    """Collect heavy-lock operations against HOT_TABLES in one migration."""

    def __init__(self) -> None:
        self.hits: list[tuple[int, str, str]] = []  # (lineno, op, table)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and _is_op(func.value):
            name = func.attr
            if name in LOCKING_OPS:
                table = _first_str_arg(node)
                if table in HOT_TABLES:
                    self.hits.append((node.lineno, name, table))
            elif name == "create_index" and not _is_concurrent(node):
                # A plain CREATE INDEX takes SHARE: it blocks writes for the
                # duration. CONCURRENTLY does not, so it is exempt.
                table = _nth_str_arg(node, 1)
                if table in HOT_TABLES:
                    self.hits.append((node.lineno, "create_index", table))
        self.generic_visit(node)


def _is_op(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "op"


def _is_concurrent(node: ast.Call) -> bool:
    return any(
        kw.arg == "postgresql_concurrently"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value is True
        for kw in node.keywords
    )


def _first_str_arg(node: ast.Call) -> str | None:
    return _nth_str_arg(node, 0)


def _nth_str_arg(node: ast.Call, index: int) -> str | None:
    if len(node.args) > index:
        arg = node.args[index]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    return None


def _string_constants(tree: ast.AST) -> list[tuple[int, str]]:
    return [
        (n.lineno, n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def check(path: Path) -> list[str]:
    """Return human-readable violations for one migration file."""
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except SyntaxError as exc:
        return [f"{path}: could not parse ({exc})"]

    finder = _Finding()
    finder.visit(tree)
    hits = list(finder.hits)

    strings = _string_constants(tree)
    # Raw SQL reaches the same locks without going through an op.* helper.
    for lineno, text in strings:
        for table in _ALTER_TABLE_SQL.findall(text):
            if table in HOT_TABLES:
                hits.append((lineno, "raw ALTER TABLE", table))

    if not hits:
        return []
    if any("lock_timeout" in text for _, text in strings):
        return []

    return [
        f"{path}:{lineno}  {op} on hot table `{table}`"
        for lineno, op, table in sorted(hits)
    ]


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv[1:] if a.endswith(".py")]
    if not paths:
        print("migration-lock-guard: no migration files to check.")
        return 0

    violations: list[str] = []
    for path in paths:
        if path.exists():
            violations.extend(check(path))

    if not violations:
        print(f"migration-lock-guard: {len(paths)} migration(s) checked, all clear.")
        return 0

    print("Migration(s) take a heavy lock on a hot table without a lock_timeout:\n")
    for violation in violations:
        print(f"  {violation}")
    print(
        "\nThese run against PRODUCTION on merge to main, while the app is live.\n"
        "Without a bounded wait, a blocked migration queues every query on that\n"
        "table behind its pending lock.\n"
        "\nAdd a bounded wait at the top of upgrade():\n"
        "    op.execute(\"SET LOCAL lock_timeout = '5s'\")\n"
        "\nAlso check lock ORDERING. Live traffic reaches these tables in a fixed\n"
        "order; acquiring them in a different order deadlocks against it. Taking\n"
        "the heaviest lock first, while holding nothing else, avoids the cycle:\n"
        '    op.execute("LOCK TABLE tasks IN ACCESS EXCLUSIVE MODE")\n'
        "\nSee oddish/alembic/versions/prod_schema_repair_001_*.py for the shape,\n"
        "and oddish/scripts/migration_lock_guard.py for why this guard exists.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
