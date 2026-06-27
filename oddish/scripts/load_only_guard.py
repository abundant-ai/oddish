#!/usr/bin/env python3
"""CI guard: enforce ``load_only()`` coverage for FE-surfaced model columns.

Why this exists
---------------
``list_tasks_core`` (``oddish/core/endpoints/tasks_query.py``) powers every
``/tasks`` route, including the experiment/dashboard views. Its **compact** path
(``compact_trials=True``) restricts the trial/task/experiment selectin loads with
``load_only(...)``, which makes *only* the enumerated columns eager and **defers
everything else**. Under async SQLAlchemy, reading a deferred column inside a
response builder fires a lazy-load outside the request greenlet and 500s with
``sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called``.

Concretely: surfacing a new column in the compact builder (e.g.
``harbor_sha=trial.harbor_sha``) without adding it to the trial ``load_only``
set leaves that column deferred, so the builder's read lazy-loads outside the
greenlet and 500s every ``GET /tasks`` -- blanking the experiment/dashboard
views. Builder unit tests can't catch this: in-memory model instances have every
attribute set, so the deferred-column lazy-load never fires off a live session.
The bug lives in the *query options*, not the builder.

What this guard does
--------------------
It statically diffs the columns **read** on the compact response-builder path
against the columns **declared** in the matching ``load_only(...)`` sets, and
fails when a read column is missing from its set.

* **Models are derived, not hardcoded.** The set of models to check is whatever
  appears in the ``load_only(...)`` sets in ``list_tasks_core`` (today
  ``TrialModel`` / ``TaskModel`` / ``ExperimentModel``). A model restricted by a
  new ``load_only`` set is picked up automatically -- a model with *no*
  ``load_only`` is not checked because it can't defer (it's fully loaded, so no
  ``MissingGreenlet``). Each discovered model is introspected via SQLAlchemy for
  its real column names, relationship names, and primary key. (``load_only``
  never defers a primary key, so PKs are always allowed even when unlisted.)
* **The compact builder entry point is derived, not named.** The guard finds the
  ``*_compact`` builder call inside ``list_tasks_core`` (today
  ``build_task_status_response_compact``) and walks the call graph from there,
  following only module-local calls. The full (non-compact) builders --
  ``build_trial_response`` / ``build_task_status_response`` -- are intentionally
  *not* reachable from this entry: the non-compact path applies no ``load_only``,
  so its reads are unconstrained and must not be diffed against the compact sets
  (they legitimately read columns the compact sets omit, e.g. ``trial.result``).
  ``_build_task_status_response`` *is* reachable and shared by both paths, so its
  task/experiment reads are checked against the compact sets.
* Within each reachable function, binds local names to models from parameter
  annotations, model-returning helpers, and iteration over model relationships,
  then records every ``model.column`` and ``getattr(model, "column")`` read.
* Reports ``read - declared - primary_key`` per model.

Tripwire
--------
The guard is scoped to the single ``load_only`` site that exists today
(``list_tasks_core``). To stop a *new* site from silently shipping uncovered, a
tripwire scans ``oddish/src/oddish`` for any ``load_only(...)`` outside
``list_tasks_core`` and fails -- prompting whoever added it to extend the guard.
(``backend/`` is a separate package and is not scanned.)

Run ``python scripts/load_only_guard.py`` from the ``oddish`` package root.
Exits non-zero (with a report) on any uncovered column or any stray
``load_only`` site.
"""

from __future__ import annotations

import ast
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import inspect as sa_inspect

_REPO_ODDISH_ROOT = Path(__file__).resolve().parents[1]

# Resolve ``oddish`` against this working tree's ``src`` regardless of how the
# guard is invoked (direct, pre-commit, CI, or as an imported module), so the
# introspected models always match the code being checked.
_SRC_ROOT = _REPO_ODDISH_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

import oddish.db as _oddish_db  # noqa: E402  (after sys.path setup)

_PKG_ROOT = _SRC_ROOT / "oddish"
_HELPERS_PATH = _PKG_ROOT / "core" / "helpers.py"
_TASKS_QUERY_PATH = _PKG_ROOT / "core" / "endpoints" / "tasks_query.py"

# The function holding the compact-path query + its ``load_only`` sets. This is
# the guard's one structural anchor; if it moves, the guard fails loudly rather
# than silently checking nothing.
_QUERY_FUNCTION = "list_tasks_core"

# Compact builder calls are recognised by this name suffix (repo convention:
# ``build_task_status_response_compact``). The actual entry-point name is derived
# from the call site in ``list_tasks_core`` rather than hardcoded, so a renamed
# or additional ``*_compact`` builder is picked up automatically.
_COMPACT_SUFFIX = "_compact"

# Builtins that wrap an iterable but preserve element type (``list(xs)``,
# ``sorted(xs)``, ...). Used to see through wrappers to the underlying
# ``model.relationship`` so iteration targets bind to the related model.
_SEQUENCE_WRAPPERS = {"list", "tuple", "set", "frozenset", "sorted", "reversed"}


@dataclass(frozen=True)
class _ModelMeta:
    name: str
    columns: frozenset[str]
    primary_key: frozenset[str]
    # relationship name -> (related model class name, is_collection)
    relationships: dict[str, tuple[str, bool]]


@dataclass(frozen=True)
class _InferType:
    """Inferred type of an expression: a model, optionally as a collection."""

    model_name: str
    is_collection: bool


def introspect_models(model_names: set[str]) -> dict[str, _ModelMeta]:
    """Map each model name to column/PK/relationship metadata via SQLAlchemy.

    ``model_names`` is derived from the ``load_only`` sets, so a model can only be
    checked if it's actually restricted by one. Each name must resolve to a mapped
    class exported from ``oddish.db``; anything else fails loudly.
    """
    metas: dict[str, _ModelMeta] = {}
    for name in sorted(model_names):
        model = getattr(_oddish_db, name, None)
        if model is None:
            raise SystemExit(
                f"load_only_guard: load_only references {name!r}, which is not "
                f"exported from oddish.db. Cannot introspect it."
            )
        try:
            insp = sa_inspect(model)
            column_attrs = insp.column_attrs
        except Exception as exc:  # noqa: BLE001 - report any non-mapped class
            raise SystemExit(
                f"load_only_guard: {name!r} is not a SQLAlchemy mapped model "
                f"({exc})."
            ) from exc
        metas[name] = _ModelMeta(
            name=name,
            columns=frozenset(c.key for c in column_attrs),
            primary_key=frozenset(c.name for c in insp.primary_key),
            relationships={
                rel.key: (rel.mapper.class_.__name__, bool(rel.uselist))
                for rel in insp.relationships
            },
        )
    return metas


def _annotation_type(
    node: ast.expr | None, metas: dict[str, _ModelMeta]
) -> _InferType | None:
    """Resolve a type annotation node to a model type, if it names one of ours.

    Handles ``TrialModel``, ``"TrialModel"`` (forward ref), ``X | None``,
    ``Optional[X]``, and ``list[X]`` / ``Sequence[X]`` style wrappers.
    """
    if node is None:
        return None
    if isinstance(node, ast.Name):
        if node.id in metas:
            return _InferType(node.id, False)
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        # Forward reference like "TrialModel". Parse the inner expression.
        try:
            inner = ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return None
        return _annotation_type(inner, metas)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        # ``X | None`` -- return whichever side names a model.
        return _annotation_type(node.left, metas) or _annotation_type(node.right, metas)
    if isinstance(node, ast.Subscript):
        base = node.value
        base_name = base.id if isinstance(base, ast.Name) else None
        if isinstance(base, ast.Attribute):
            base_name = base.attr
        inner_node = node.slice
        if base_name in {"Optional"}:
            return _annotation_type(inner_node, metas)
        if base_name in {
            "list",
            "List",
            "Sequence",
            "Iterable",
            "tuple",
            "Tuple",
            "set",
            "Set",
        }:
            # Element type may be a tuple (``tuple[X, ...]``); take the first.
            if isinstance(inner_node, ast.Tuple) and inner_node.elts:
                inner_node = inner_node.elts[0]
            element = _annotation_type(inner_node, metas)
            if element is not None:
                return _InferType(element.model_name, True)
        return None
    return None


def _function_return_types(
    tree: ast.AST, metas: dict[str, _ModelMeta]
) -> dict[str, _InferType]:
    """Map module-local function name -> inferred model return type (if any)."""
    returns: dict[str, _InferType] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            inferred = _annotation_type(node.returns, metas)
            if inferred is not None:
                returns[node.name] = inferred
    return returns


@dataclass
class _ReadCollector:
    metas: dict[str, _ModelMeta]
    func_returns: dict[str, _InferType]
    bindings: dict[str, _InferType] = field(default_factory=dict)

    def infer(self, node: ast.expr) -> _InferType | None:
        if isinstance(node, ast.Name):
            return self.bindings.get(node.id)
        if isinstance(node, ast.Attribute):
            base = self.infer(node.value)
            if base is None or base.is_collection:
                return None
            meta = self.metas.get(base.model_name)
            if meta is None:
                return None
            rel = meta.relationships.get(node.attr)
            if rel is not None:
                related_name, is_collection = rel
                return _InferType(related_name, is_collection)
            return None
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                if func.id in _SEQUENCE_WRAPPERS and node.args:
                    inner = self.infer(node.args[0])
                    if inner is not None:
                        return _InferType(inner.model_name, True)
                    return None
                if func.id in self.func_returns:
                    return self.func_returns[func.id]
            return None
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            # ``task.experiments or []`` -- first operand that yields a type.
            for value in node.values:
                inferred = self.infer(value)
                if inferred is not None:
                    return inferred
            return None
        if isinstance(node, ast.IfExp):
            return self.infer(node.body) or self.infer(node.orelse)
        if isinstance(node, ast.Subscript):
            base = self.infer(node.value)
            if base is not None and base.is_collection:
                return _InferType(base.model_name, False)
            return None
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            element = self.infer(node.elt)
            if element is not None and not element.is_collection:
                return _InferType(element.model_name, True)
            return None
        return None

    def _binding_sites(self, fn: ast.AST) -> list[tuple[str, ast.expr]]:
        """Collect ``(name, value_expr)`` pairs that bind a local name.

        Covers assignments, ``for`` targets, and comprehension generators. The
        caller resolves the value's type to a fixpoint (a name may depend on a
        name bound by an earlier site).
        """
        sites: list[tuple[str, ast.expr]] = []

        def iter_target(target: ast.expr, value: ast.expr) -> None:
            if isinstance(target, ast.Name):
                sites.append((target.id, value))

        for node in ast.walk(fn):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    iter_target(target, node.value)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                iter_target(node.target, node.value)
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                self._iter_loop_target(node.target, node.iter, sites)
            elif isinstance(
                node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
            ):
                for gen in node.generators:
                    self._iter_loop_target(gen.target, gen.iter, sites)
        return sites

    def _iter_loop_target(
        self, target: ast.expr, iter_expr: ast.expr, sites: list[tuple[str, ast.expr]]
    ) -> None:
        # Wrap the iterable so its element type (not the collection) binds the
        # loop variable. ``_LoopElement`` is unwrapped in ``_resolve_bindings``.
        if isinstance(target, ast.Name):
            sites.append((target.id, _LoopElement(iter_expr)))

    def _resolve_bindings(self, fn: ast.AST) -> None:
        sites = self._binding_sites(fn)
        # Fixpoint: re-resolve until no new binding appears. Bounded by the
        # number of sites + 1 so a pathological chain still terminates.
        for _ in range(len(sites) + 1):
            changed = False
            for name, value in sites:
                if isinstance(value, _LoopElement):
                    inferred = self.infer(value.iterable)
                    if inferred is not None and inferred.is_collection:
                        inferred = _InferType(inferred.model_name, False)
                    else:
                        inferred = None
                else:
                    inferred = self.infer(value)
                if inferred is not None and self.bindings.get(name) != inferred:
                    self.bindings[name] = inferred
                    changed = True
            if not changed:
                break

    def collect(self, fn: ast.AST) -> set[tuple[str, str]]:
        """Return ``(model_name, column)`` pairs read inside ``fn``.

        ``self.bindings`` must already be seeded with this function's parameter
        types before calling.
        """
        self._resolve_bindings(fn)
        reads: set[tuple[str, str]] = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                base = self.infer(node.value)
                if base is None or base.is_collection:
                    continue
                meta = self.metas.get(base.model_name)
                if meta is not None and node.attr in meta.columns:
                    reads.add((base.model_name, node.attr))
            elif isinstance(node, ast.Call):
                read = self._getattr_read(node)
                if read is not None:
                    reads.add(read)
        return reads

    def _getattr_read(self, node: ast.Call) -> tuple[str, str] | None:
        """Detect ``getattr(model_expr, "column")`` reads (string-literal attr)."""
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "getattr"):
            return None
        if len(node.args) < 2:
            return None
        base = self.infer(node.args[0])
        attr_node = node.args[1]
        if base is None or base.is_collection:
            return None
        if not (
            isinstance(attr_node, ast.Constant) and isinstance(attr_node.value, str)
        ):
            return None
        meta = self.metas.get(base.model_name)
        if meta is not None and attr_node.value in meta.columns:
            return (base.model_name, attr_node.value)
        return None


@dataclass(frozen=True)
class _LoopElement:
    """Marker: bind a loop target to the *element* type of ``iterable``."""

    iterable: ast.expr


def _find_function(tree: ast.AST, name: str) -> ast.AST | None:
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            return node
    return None


def _is_load_only_call(node: ast.AST) -> bool:
    """True for ``load_only(...)`` and ``loader.load_only(...)`` calls."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id == "load_only") or (
        isinstance(func, ast.Attribute) and func.attr == "load_only"
    )


def _require_query_function(tasks_query_path: Path) -> ast.AST:
    tree = ast.parse(tasks_query_path.read_text(), filename=str(tasks_query_path))
    fn = _find_function(tree, _QUERY_FUNCTION)
    if fn is None:
        raise SystemExit(
            f"load_only_guard: {_QUERY_FUNCTION}() not found in "
            f"{tasks_query_path.name}. Did it get renamed or moved?"
        )
    return fn


def derive_compact_entries(tasks_query_path: Path = _TASKS_QUERY_PATH) -> set[str]:
    """Names of the ``*_compact`` builder(s) called inside ``list_tasks_core``.

    The compact response builders are recognised by the ``_compact`` suffix and
    discovered from their call sites, so the guard tracks renames / additions
    without a hardcoded entry-point name.
    """
    fn = _require_query_function(tasks_query_path)
    entries: set[str] = set()
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id.endswith(_COMPACT_SUFFIX)
        ):
            entries.add(node.func.id)
    if not entries:
        raise SystemExit(
            f"load_only_guard: no '*{_COMPACT_SUFFIX}' builder call found in "
            f"{_QUERY_FUNCTION}(). The compact builder may have been renamed; "
            f"the guard recognises compact builders by the '{_COMPACT_SUFFIX}' "
            f"suffix."
        )
    return entries


def _reachable_functions(tree: ast.AST, entries: set[str]) -> dict[str, ast.AST]:
    """Return ``{name: FunctionDef}`` reachable from ``entries`` via local calls."""
    local_funcs: dict[str, ast.AST] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            local_funcs[node.name] = node
    missing = entries - local_funcs.keys()
    if missing:
        raise SystemExit(
            f"load_only_guard: compact entry point(s) {sorted(missing)!r} called "
            f"in {_QUERY_FUNCTION}() but not defined in helpers.py. Builders "
            f"refactored to another module? Point the guard at it."
        )
    reachable: dict[str, ast.AST] = {}
    queue = list(entries)
    while queue:
        name = queue.pop()
        if name in reachable:
            continue
        fn = local_funcs[name]
        reachable[name] = fn
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                callee = sub.func.id
                if callee in local_funcs and callee not in reachable:
                    queue.append(callee)
    return reachable


def collect_read_columns(
    helpers_path: Path,
    metas: dict[str, _ModelMeta],
    entries: set[str],
) -> dict[str, set[str]]:
    """Columns read on the compact builder path, bucketed by model name."""
    tree = ast.parse(helpers_path.read_text(), filename=str(helpers_path))
    func_returns = _function_return_types(tree, metas)
    reachable = _reachable_functions(tree, entries)

    reads: dict[str, set[str]] = defaultdict(set)
    for fn in reachable.values():
        collector = _ReadCollector(metas=metas, func_returns=func_returns)
        for arg in _all_args(fn):
            if arg.annotation is not None:
                inferred = _annotation_type(arg.annotation, metas)
                if inferred is not None:
                    collector.bindings[arg.arg] = inferred
        for model_name, column in collector.collect(fn):
            reads[model_name].add(column)
    return reads


def _all_args(fn: ast.AST) -> list[ast.arg]:
    args = fn.args  # type: ignore[attr-defined]
    return [*args.posonlyargs, *args.args, *args.kwonlyargs]


def collect_declared_columns(
    tasks_query_path: Path = _TASKS_QUERY_PATH,
) -> dict[str, set[str]]:
    """Columns declared in the ``load_only(...)`` sets inside ``list_tasks_core``.

    Buckets every ``Model.column`` argument to any ``load_only`` call (both the
    bare ``load_only(...)`` and the ``loader.load_only(...)`` forms) by model
    name. The model set is whatever appears here -- nothing is hardcoded.
    """
    fn = _require_query_function(tasks_query_path)
    declared: dict[str, set[str]] = defaultdict(set)
    for node in ast.walk(fn):
        if not _is_load_only_call(node):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name):
                declared[arg.value.id].add(arg.attr)
    if not declared:
        raise SystemExit(
            f"load_only_guard: no load_only(Model.column, ...) calls found in "
            f"{_QUERY_FUNCTION}(). The compact load_only sets may have moved."
        )
    return dict(declared)


def compute_violations(
    reads: dict[str, set[str]],
    declared: dict[str, set[str]],
    metas: dict[str, _ModelMeta],
) -> dict[str, list[str]]:
    """Read columns missing from their ``load_only`` set, bucketed by model.

    Primary keys are never deferred by ``load_only``, so they're always allowed
    even if unlisted.
    """
    violations: dict[str, list[str]] = {}
    for model_name, read_cols in reads.items():
        meta = metas[model_name]
        missing = read_cols - declared.get(model_name, set()) - meta.primary_key
        if missing:
            violations[model_name] = sorted(missing)
    return violations


def find_violations(
    helpers_path: Path = _HELPERS_PATH,
    tasks_query_path: Path = _TASKS_QUERY_PATH,
) -> dict[str, list[str]]:
    declared = collect_declared_columns(tasks_query_path)
    metas = introspect_models(set(declared))
    entries = derive_compact_entries(tasks_query_path)
    reads = collect_read_columns(helpers_path, metas, entries)
    return compute_violations(reads, declared, metas)


def _load_only_sites_in_tree(tree: ast.AST) -> list[tuple[str | None, int]]:
    """``(enclosing_function_name, lineno)`` for each load_only call in ``tree``.

    ``enclosing_function_name`` is the *nearest* enclosing def (``None`` at module
    scope).
    """
    sites: list[tuple[str | None, int]] = []

    def visit(node: ast.AST, func_name: str | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_name = node.name
        if _is_load_only_call(node):
            sites.append((func_name, node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child, func_name)

    visit(tree, None)
    return sites


def find_stray_load_only_sites(
    src_root: Path = _PKG_ROOT,
    allowed_file: Path = _TASKS_QUERY_PATH,
    allowed_function: str = _QUERY_FUNCTION,
) -> list[tuple[Path, str | None, int]]:
    """Find ``load_only`` calls outside the single covered site (the tripwire).

    Scans every ``*.py`` under ``src_root`` and returns ``(path, function,
    lineno)`` for any ``load_only`` not at ``(allowed_file, allowed_function)``.
    A non-empty result means a new ``load_only`` site shipped that the column
    check does not cover -- the guard must be extended to it.
    """
    allowed_file = allowed_file.resolve()
    strays: list[tuple[Path, str | None, int]] = []
    for path in sorted(src_root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            # Unparseable files can't run, so they can't host a live load_only.
            continue
        for func_name, lineno in _load_only_sites_in_tree(tree):
            if path.resolve() == allowed_file and func_name == allowed_function:
                continue
            strays.append((path, func_name, lineno))
    return strays


def main() -> int:
    violations = find_violations()
    strays = find_stray_load_only_sites()
    if not violations and not strays:
        models = ", ".join(sorted(introspect_models(set(collect_declared_columns()))))
        print(
            "load_only_guard: OK -- every column read on the compact /tasks path "
            f"is covered by its load_only() set (models: {models}); no stray "
            "load_only sites."
        )
        return 0

    print("load_only_guard: FAILED\n", file=sys.stderr)

    if violations:
        print(
            "The compact /tasks response builders read these columns, but they "
            "are\nmissing from the matching load_only() set in list_tasks_core\n"
            "(oddish/core/endpoints/tasks_query.py). Under async SQLAlchemy each "
            "one\nwould lazy-load outside the request greenlet and 500 every GET "
            "/tasks\nwith MissingGreenlet.\n",
            file=sys.stderr,
        )
        for model_name in sorted(violations):
            for column in violations[model_name]:
                print(
                    f"  - {model_name}.{column}  ->  add to the "
                    f"`load_only({model_name}.*)` set",
                    file=sys.stderr,
                )
        print(
            "\nFix: add each column above to its load_only(...) set, OR stop "
            "reading it\nin the compact builders.",
            file=sys.stderr,
        )

    if strays:
        if violations:
            print(file=sys.stderr)
        print(
            "New load_only() site(s) found outside list_tasks_core. The guard "
            "only\ncovers list_tasks_core, so these are NOT checked for "
            "MissingGreenlet. Extend\nthe guard to cover them (or confirm they "
            "need no compact-column coverage):\n",
            file=sys.stderr,
        )
        for path, func_name, lineno in strays:
            rel = path.resolve().relative_to(_REPO_ODDISH_ROOT.parent)
            where = f"{func_name}()" if func_name else "<module scope>"
            print(f"  - {rel}:{lineno}  in {where}", file=sys.stderr)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
