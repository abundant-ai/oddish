"""Guardrail: no new S3 delete call sites without explicit review.

Preview branches now run against clones of prod data. That raises the
cost of any code path that deletes S3 objects: a regression that takes
a delete path in preview would target the *same* prod bucket. To make
that class of mistake hard to slip in unnoticed, this test enumerates
every ``delete_object`` / ``delete_objects`` / ``delete_prefix`` call
site in ``oddish/src`` and fails if a new one shows up that isn't on
the allowlist.

Adding a new entry is a deliberate, reviewer-visible change: update
``ALLOWED_DELETE_SITES`` below with the file + enclosing function and
note in the PR description what user-data scope the new delete touches
and why it's safe in preview. If you can scope the delete to objects
under ``settings.s3_write_prefix``, do that instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"

# Method names that, when called, perform an S3 delete. ``delete_prefixes``
# and ``delete_s3_prefixes`` are intentionally *not* listed: they are
# bulk wrappers around ``delete_prefix`` and adding a new caller of them
# is reviewed via the existing endpoint surface, not here.
DELETE_METHOD_NAMES = frozenset({"delete_object", "delete_objects", "delete_prefix"})

# (relative_path_under_src, enclosing_function_name) — append to extend.
ALLOWED_DELETE_SITES: frozenset[tuple[str, str]] = frozenset(
    {
        # Cleans up the per-import staging tarball it itself wrote.
        # Transient, never user content.
        ("oddish/db/storage.py", "extract_trial_import_archive"),
        # Implementations of the bulk-delete primitives. ``delete_prefix``
        # uses ``delete_objects``; ``delete_prefixes`` calls
        # ``delete_prefix``. These are the seams every higher-level
        # delete flows through, so callers concentrate here.
        ("oddish/db/storage.py", "delete_prefix"),
        ("oddish/db/storage.py", "delete_prefixes"),
    }
)


def _find_delete_calls(tree: ast.AST) -> list[tuple[str, int]]:
    """Return ``(enclosing_function, lineno)`` for each delete call in *tree*."""
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    def enclosing_function_name(node: ast.AST) -> str | None:
        cursor: ast.AST | None = node
        while cursor is not None:
            if isinstance(cursor, (ast.AsyncFunctionDef, ast.FunctionDef)):
                return cursor.name
            cursor = parents.get(cursor)
        return None

    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match attribute calls (``self._s3.delete_object``, ``storage.delete_prefix``)
        # *and* bare-name calls (``delete_prefix(...)``) so a refactor that
        # imports ``delete_prefix`` directly still gets caught.
        name: str | None = None
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        if name not in DELETE_METHOD_NAMES:
            continue
        enclosing = enclosing_function_name(node)
        if enclosing is None:
            enclosing = "<module>"
        hits.append((enclosing, node.lineno))
    return hits


def test_no_unexpected_delete_call_sites() -> None:
    found: set[tuple[str, str]] = set()
    for path in SRC_ROOT.rglob("*.py"):
        rel = path.relative_to(SRC_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for enclosing, _lineno in _find_delete_calls(tree):
            # Skip the call inside the ``delete_prefix`` *method definition*
            # itself when the enclosing function is also called
            # ``delete_prefix`` — that's the implementation, allowlisted.
            found.add((rel, enclosing))

    unexpected = found - ALLOWED_DELETE_SITES
    stale = ALLOWED_DELETE_SITES - found
    assert not unexpected, (
        "New S3 delete call site detected. If this delete is intentional, "
        "add it to ALLOWED_DELETE_SITES with an explanatory comment in the "
        f"PR. New sites: {sorted(unexpected)}"
    )
    assert not stale, (
        "ALLOWED_DELETE_SITES references a site that no longer exists; "
        "remove it from the allowlist. Stale: " + repr(sorted(stale))
    )
