#!/usr/bin/env python3
"""CI guard: enforce ``load_only()`` coverage for FE-surfaced model columns.

Why this exists
---------------
The experiment/dashboard/``/tasks`` list endpoints restrict their trial/task/
experiment loads with ``load_only(...)``, which makes *only* the enumerated
columns eager and **defers everything else**. Under async SQLAlchemy, reading a
deferred column inside a response builder fires a lazy-load outside the request
greenlet and 500s with ``sqlalchemy.exc.MissingGreenlet``. Surfacing a new
column in a builder without adding it to the matching ``load_only`` set is
therefore a latent prod 500 that builder unit tests can't catch (in-memory
instances have every attribute set). The bug lives in the *query options*.

What this guard does
--------------------
Each entry in ``_COVERAGE_UNITS`` pairs a query function (holding the
``load_only`` projections) with the builder entry points that run under them.
For every unit the guard statically diffs the columns **read** on that builder
path against the columns **declared** in that unit's ``load_only`` sets, and
fails on any read column that isn't declared.

* ``load_only(Model.column, ...)`` and ``load_only(*COLUMN_TUPLE)`` are both
  understood; module-level ``COLUMN_TUPLE = (Model.column, ...)`` constants are
  resolved from helpers.py / tasks_query.py.
* Models are derived from the ``load_only`` sets: a model with no ``load_only``
  in a unit is fully loaded (can't defer) and is not checked for that unit.
  Introspected via SQLAlchemy for real column/PK/relationship names.
* From each unit's builder entries the guard walks module-local calls, binds
  locals to models via annotations / model-returning helpers / relationship
  iteration, and records every ``model.column`` and ``getattr`` read. PKs are
  never deferred by ``load_only`` so they're always allowed.

``_SCHEMA_UNITS`` covers the other builder shape: paths whose response is built
by ``SomeResponse.model_validate(row)`` rather than a hand-written builder.
Pydantic reads through ``from_attributes``, so there is no ``model.column``
expression to walk -- the read set is the schema's own fields instead. Same
MissingGreenlet failure, reached by a path the AST walker cannot see.

Tripwire
--------
A stray ``load_only(...)`` outside the covered functions ships uncovered. The
tripwire scans ``oddish/src/oddish`` and fails on any such site, prompting a new
``_COVERAGE_UNITS`` / ``_SCHEMA_UNITS`` entry. (``backend/`` is a separate
package, not scanned -- a schema unit is how a ``backend/`` router's reads get
checked against a ``load_only`` that lives here.)

Run ``python scripts/load_only_guard.py`` from the ``oddish`` package root.
Exits non-zero (with a report) on any uncovered column or stray ``load_only``.
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
# guard is invoked (direct or in CI), so the introspected models always match
# the code being checked.
_SRC_ROOT = _REPO_ODDISH_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

import oddish.db as _oddish_db  # noqa: E402  (after sys.path setup)
import oddish.schemas as _oddish_schemas  # noqa: E402  (after sys.path setup)

_PKG_ROOT = _SRC_ROOT / "oddish"
_HELPERS_PATH = _PKG_ROOT / "core" / "helpers.py"
_TASKS_QUERY_PATH = _PKG_ROOT / "core" / "endpoints" / "tasks_query.py"

# ``(query_function, builder_entry_points)``. Each query function holds the
# ``load_only`` projections for one list path; the builders are the response
# builders that run under them. A read column missing from a unit's load_only
# set 500s that path with MissingGreenlet. Add a load_only site => add a unit
# (the tripwire fails until you do).
_COVERAGE_UNITS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("list_tasks_core", ("build_task_status_response_compact",)),
    ("list_experiment_task_shells_core", ("build_task_status_responses_from_counts",)),
    ("list_experiment_slim_tasks", ("build_slim_task_status_response",)),
)
_COVERED_FUNCTIONS = frozenset(fn for fn, _ in _COVERAGE_UNITS)


# Schema units: query functions whose "builder" is a Pydantic response model
# rather than a hand-written builder. ``model_validate(row)`` reads attributes
# through ``from_attributes``, so there is no ``model.column`` expression for the
# AST walker above to find -- the read set comes from the schema's fields
# instead. ``(query_function, query_path, model_name, response_schema)``.
# ``non_column_fields`` are schema fields deliberately not backed by a column on
# the model -- they're populated some other way and read nothing off the row.
# Every other field must be a column, so a renamed or typo'd field fails loudly
# instead of silently dropping out of coverage.
_SCHEMA_UNITS: tuple[tuple[str, Path, str, str, frozenset[str]], ...] = (
    (
        "list_analyzers_core",
        _PKG_ROOT / "core" / "analyzers.py",
        "AnalyzerModel",
        "ReportResponse",
        # Filled by a separate query in the router's _to_response().
        frozenset({"experiment_ids"}),
    ),
)

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


def _collect_module_tuple_columns(*paths: Path) -> dict[str, dict[str, set[str]]]:
    """Map module-level ``NAME = (Model.column, ...)`` tuples to their columns.

    Lets ``load_only(*NAME)`` resolve to the same ``{model: {columns}}`` shape as
    an inline ``load_only(Model.column, ...)``.
    """
    tuples: dict[str, dict[str, set[str]]] = {}
    for path in paths:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            if not isinstance(value, (ast.Tuple, ast.List)):
                continue
            cols: dict[str, set[str]] = defaultdict(set)
            for elt in value.elts:
                if isinstance(elt, ast.Attribute) and isinstance(elt.value, ast.Name):
                    cols[elt.value.id].add(elt.attr)
            if not cols:
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    tuples[target.id] = {m: set(c) for m, c in cols.items()}
    return tuples


def collect_declared_columns(
    query_fn: ast.AST,
    tuple_columns: dict[str, dict[str, set[str]]],
) -> dict[str, set[str]]:
    """Columns declared in the ``load_only(...)`` sets inside ``query_fn``.

    Buckets every ``Model.column`` argument (inline or via a resolved
    ``*COLUMN_TUPLE``) to any ``load_only`` call, by model name.
    """
    declared: dict[str, set[str]] = defaultdict(set)
    for node in ast.walk(query_fn):
        if not _is_load_only_call(node):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name):
                declared[arg.value.id].add(arg.attr)
            elif isinstance(arg, ast.Starred) and isinstance(arg.value, ast.Name):
                for model_name, cols in tuple_columns.get(arg.value.id, {}).items():
                    declared[model_name].update(cols)
    return dict(declared)


def _reachable_functions(tree: ast.AST, entries: set[str]) -> dict[str, ast.AST]:
    """Return ``{name: FunctionDef}`` reachable from ``entries`` via local calls."""
    local_funcs: dict[str, ast.AST] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            local_funcs[node.name] = node
    missing = entries - local_funcs.keys()
    if missing:
        raise SystemExit(
            f"load_only_guard: builder entry point(s) {sorted(missing)!r} are not "
            f"defined in helpers.py. Renamed or moved to another module? Update "
            f"_COVERAGE_UNITS."
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
    """Columns read on the builder path from ``entries``, bucketed by model name."""
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


def _iter_units(
    helpers_path: Path, tasks_query_path: Path
) -> list[tuple[str, tuple[str, ...], dict[str, set[str]], dict[str, _ModelMeta]]]:
    """Resolve each coverage unit to ``(function, entries, declared, metas)``."""
    tuple_columns = _collect_module_tuple_columns(helpers_path, tasks_query_path)
    tasks_tree = ast.parse(tasks_query_path.read_text(), filename=str(tasks_query_path))
    units = []
    for query_function, entries in _COVERAGE_UNITS:
        query_fn = _find_function(tasks_tree, query_function)
        if query_fn is None:
            raise SystemExit(
                f"load_only_guard: {query_function}() not found in "
                f"{tasks_query_path.name}. Renamed or moved? Update _COVERAGE_UNITS."
            )
        declared = collect_declared_columns(query_fn, tuple_columns)
        if not declared:
            raise SystemExit(
                f"load_only_guard: no load_only(...) columns found in "
                f"{query_function}(). Its load_only projections may have moved."
            )
        metas = introspect_models(set(declared))
        units.append((query_function, entries, declared, metas))
    return units


def find_violations(
    helpers_path: Path = _HELPERS_PATH,
    tasks_query_path: Path = _TASKS_QUERY_PATH,
) -> dict[str, dict[str, list[str]]]:
    """Per-unit column violations: ``{query_function: {model: [missing cols]}}``."""
    all_violations: dict[str, dict[str, list[str]]] = {}
    for query_function, entries, declared, metas in _iter_units(
        helpers_path, tasks_query_path
    ):
        reads = collect_read_columns(helpers_path, metas, set(entries))
        violations = compute_violations(reads, declared, metas)
        if violations:
            all_violations[query_function] = violations
    return all_violations


def schema_read_columns(
    response_schema: str, meta: _ModelMeta, non_column_fields: frozenset[str]
) -> set[str]:
    """Columns a Pydantic response model reads off ``meta`` via model_validate.

    ``AnalyzerResponse.model_validate(analyzer)`` runs with ``from_attributes``,
    so every field it declares is an attribute read on the ORM row. A field whose
    name is a deferred column lazy-loads outside the request greenlet and 500s the
    list response -- the same failure the AST walker catches for hand-written
    builders, arriving through a path the walker cannot see.

    Any field that is not a column must be declared in ``non_column_fields``, and
    every declared exemption must still be a field. Otherwise a renamed field
    reads as "not a column" and drops out of coverage silently -- the guard would
    go green on exactly the change it exists to catch.
    """
    schema = getattr(_oddish_schemas, response_schema, None)
    if schema is None:
        raise SystemExit(
            f"load_only_guard: response schema {response_schema!r} is not exported "
            f"from oddish.schemas. Renamed or moved? Update _SCHEMA_UNITS."
        )
    fields = set(schema.model_fields)

    undeclared = fields - meta.columns - non_column_fields
    if undeclared:
        raise SystemExit(
            f"load_only_guard: {response_schema} field(s) {sorted(undeclared)!r} "
            f"are not columns on {meta.name}. If populated outside the row, add "
            f"them to that unit's non_column_fields in _SCHEMA_UNITS; if renamed, "
            f"fix the name."
        )
    stale = non_column_fields - fields
    if stale:
        raise SystemExit(
            f"load_only_guard: _SCHEMA_UNITS exempts {sorted(stale)!r} on "
            f"{response_schema}, which declares no such field(s). Renamed or "
            f"removed? Drop the stale exemption."
        )
    return fields & meta.columns


def find_schema_violations() -> dict[str, dict[str, list[str]]]:
    """Per-schema-unit column violations: ``{query_function: {model: [missing]}}``."""
    tuple_columns = _collect_module_tuple_columns(_HELPERS_PATH, _TASKS_QUERY_PATH)
    all_violations: dict[str, dict[str, list[str]]] = {}
    for (
        query_function,
        query_path,
        model_name,
        response_schema,
        non_column_fields,
    ) in _SCHEMA_UNITS:
        tree = ast.parse(query_path.read_text(), filename=str(query_path))
        query_fn = _find_function(tree, query_function)
        if query_fn is None:
            raise SystemExit(
                f"load_only_guard: {query_function}() not found in "
                f"{query_path.name}. Renamed or moved? Update _SCHEMA_UNITS."
            )
        declared = collect_declared_columns(query_fn, tuple_columns)
        if not declared:
            raise SystemExit(
                f"load_only_guard: no load_only(...) columns found in "
                f"{query_function}(). Its load_only projections may have moved."
            )
        metas = introspect_models({model_name} | set(declared))
        reads = {
            model_name: schema_read_columns(
                response_schema, metas[model_name], non_column_fields
            )
        }
        violations = compute_violations(reads, declared, metas)
        if violations:
            all_violations[query_function] = violations
    return all_violations


def _covered_models(
    helpers_path: Path = _HELPERS_PATH,
    tasks_query_path: Path = _TASKS_QUERY_PATH,
) -> set[str]:
    models: set[str] = set()
    for _, _, declared, _ in _iter_units(helpers_path, tasks_query_path):
        models.update(declared)
    models.update(model_name for _, _, model_name, _, _ in _SCHEMA_UNITS)
    return models


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


def _allowed_sites() -> dict[Path, frozenset[str]]:
    """``{resolved file: covered function names}`` across both unit kinds."""
    allowed: dict[Path, set[str]] = defaultdict(set)
    allowed[_TASKS_QUERY_PATH.resolve()].update(_COVERED_FUNCTIONS)
    for query_function, query_path, _, _, _ in _SCHEMA_UNITS:
        allowed[query_path.resolve()].add(query_function)
    return {path: frozenset(fns) for path, fns in allowed.items()}


def find_stray_load_only_sites(
    src_root: Path = _PKG_ROOT,
    allowed: dict[Path, frozenset[str]] | None = None,
) -> list[tuple[Path, str | None, int]]:
    """Find ``load_only`` calls outside the covered functions (the tripwire).

    Scans every ``*.py`` under ``src_root`` and returns ``(path, function,
    lineno)`` for any ``load_only`` not inside a covered function of a covered
    file. A non-empty result means a new ``load_only`` site shipped that no
    coverage unit checks -- add one.
    """
    if allowed is None:
        allowed = _allowed_sites()
    strays: list[tuple[Path, str | None, int]] = []
    for path in sorted(src_root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            # Unparseable files can't run, so they can't host a live load_only.
            continue
        covered = allowed.get(path.resolve(), frozenset())
        for func_name, lineno in _load_only_sites_in_tree(tree):
            if func_name in covered:
                continue
            strays.append((path, func_name, lineno))
    return strays


def main() -> int:
    violations = {**find_violations(), **find_schema_violations()}
    strays = find_stray_load_only_sites()
    if not violations and not strays:
        models = ", ".join(sorted(_covered_models()))
        print(
            "load_only_guard: OK -- every column read on the covered list paths "
            f"is covered by its load_only() set (models: {models}); no stray "
            "load_only sites."
        )
        return 0

    print("load_only_guard: FAILED\n", file=sys.stderr)

    if violations:
        print(
            "These builders read columns missing from the matching load_only() "
            "set\nin their query function. Under async SQLAlchemy each one would "
            "lazy-load\noutside the request greenlet and 500 the list response "
            "with MissingGreenlet.\n",
            file=sys.stderr,
        )
        for query_function in sorted(violations):
            print(f"  {query_function}():", file=sys.stderr)
            for model_name in sorted(violations[query_function]):
                for column in violations[query_function][model_name]:
                    print(
                        f"    - {model_name}.{column}  ->  add to that path's "
                        f"`load_only({model_name}.*)` set",
                        file=sys.stderr,
                    )
        print(
            "\nFix: add each column above to the load_only(...) set for that "
            "path, OR\nstop reading it in the builder.",
            file=sys.stderr,
        )

    if strays:
        if violations:
            print(file=sys.stderr)
        print(
            "New load_only() site(s) found outside the covered functions. These "
            "are\nNOT checked for MissingGreenlet. Add a _COVERAGE_UNITS entry "
            "for the query\nfunction (or confirm it needs no compact-column "
            "coverage):\n",
            file=sys.stderr,
        )
        for path, func_name, lineno in strays:
            rel = path.resolve().relative_to(_REPO_ODDISH_ROOT.parent)
            where = f"{func_name}()" if func_name else "<module scope>"
            print(f"  - {rel}:{lineno}  in {where}", file=sys.stderr)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
