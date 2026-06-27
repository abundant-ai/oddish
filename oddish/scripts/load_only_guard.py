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

On 2026-06-24, PR #413 surfaced ``trials.harbor_sha`` in the compact builder
without adding it to the trial ``load_only`` set. Every ``GET /tasks`` 500'd for
20 minutes (1,977 errors, 23:47-00:07 UTC), blanking the experiment/dashboard
views, until #433 eager-loaded it. Builder unit tests can't catch this: in-memory
model instances have every attribute set, so the deferred-column lazy-load never
fires off a live session. The bug lives in the *query options*, not the builder.

What this guard does
--------------------
It statically diffs the columns **read** on the compact response-builder path
against the columns **declared** in the matching ``load_only(...)`` sets, and
fails when a read column is missing from its set.

* Introspects ``TrialModel`` / ``TaskModel`` / ``ExperimentModel`` to learn each
  model's real column names, relationship names, and primary key. (``load_only``
  never defers a primary key, so PKs are always allowed even when unlisted.)
* Parses ``helpers.py`` and walks the call graph starting at the compact entry
  point ``build_task_status_response_compact``, following only module-local
  calls. The full (non-compact) builders -- ``build_trial_response`` /
  ``build_task_status_response`` -- are intentionally *not* reachable from this
  entry: the non-compact path applies no ``load_only``, so its reads are
  unconstrained and must not be diffed against the compact sets (they
  legitimately read columns the compact sets omit, e.g. ``trial.result``).
  ``_build_task_status_response`` *is* reachable and shared by both paths, so its
  task/experiment reads are checked against the compact sets.
* Within each reachable function, binds local names to models from parameter
  annotations, model-returning helpers, and iteration over model relationships,
  then records every ``model.column`` and ``getattr(model, "column")`` read.
* Parses the ``load_only(...)`` argument lists inside ``list_tasks_core``,
  bucketed by model.
* Reports ``read - declared - primary_key`` per model.

Run ``python scripts/load_only_guard.py`` from the ``oddish`` package root.
Exits non-zero (with a report) if any read column is missing from its
``load_only`` set.
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

# The three models whose compact ``load_only`` sets we enforce. Reads on any
# other model (e.g. ``TaskVersionModel`` via ``task.current_version``) are out
# of scope -- they're not governed by these sets.
from oddish.db import ExperimentModel, TaskModel, TrialModel  # noqa: E402

_HELPERS_PATH = _REPO_ODDISH_ROOT / "src" / "oddish" / "core" / "helpers.py"
_TASKS_QUERY_PATH = (
    _REPO_ODDISH_ROOT / "src" / "oddish" / "core" / "endpoints" / "tasks_query.py"
)

# Entry point of the compact response-builder path. Everything reachable from
# here (via module-local calls) reads columns governed by the compact
# ``load_only`` sets. If this is ever renamed, the guard fails loudly rather
# than silently checking nothing.
_COMPACT_ENTRY = "build_task_status_response_compact"

_MODELS = {
    "TrialModel": TrialModel,
    "TaskModel": TaskModel,
    "ExperimentModel": ExperimentModel,
}

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


def introspect_models() -> dict[str, _ModelMeta]:
    """Map model name -> column/PK/relationship metadata via SQLAlchemy."""
    metas: dict[str, _ModelMeta] = {}
    for name, model in _MODELS.items():
        insp = sa_inspect(model)
        metas[name] = _ModelMeta(
            name=name,
            columns=frozenset(c.key for c in insp.column_attrs),
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


def _reachable_functions(tree: ast.AST, entry: str) -> dict[str, ast.AST]:
    """Return ``{name: FunctionDef}`` reachable from ``entry`` via local calls."""
    local_funcs: dict[str, ast.AST] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            local_funcs[node.name] = node
    if entry not in local_funcs:
        raise SystemExit(
            f"load_only_guard: compact entry point {entry!r} not found in "
            f"helpers.py. Did it get renamed? Update _COMPACT_ENTRY."
        )
    reachable: dict[str, ast.AST] = {}
    queue = [entry]
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
    helpers_path: Path, metas: dict[str, _ModelMeta]
) -> dict[str, set[str]]:
    """Columns read on the compact builder path, bucketed by model name."""
    tree = ast.parse(helpers_path.read_text(), filename=str(helpers_path))
    func_returns = _function_return_types(tree, metas)
    reachable = _reachable_functions(tree, _COMPACT_ENTRY)

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
    tasks_query_path: Path, metas: dict[str, _ModelMeta]
) -> dict[str, set[str]]:
    """Columns declared in the ``load_only(...)`` sets inside ``list_tasks_core``.

    Buckets every ``Model.column`` argument to any ``load_only`` call (both the
    bare ``load_only(...)`` and the ``loader.load_only(...)`` forms) by model.
    All three compact sets live in ``list_tasks_core``; there are no other
    ``load_only`` calls in that function.
    """
    tree = ast.parse(tasks_query_path.read_text(), filename=str(tasks_query_path))
    target_fn: ast.AST | None = None
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "list_tasks_core"
        ):
            target_fn = node
            break
    if target_fn is None:
        raise SystemExit(
            "load_only_guard: list_tasks_core not found in tasks_query.py. "
            "Did it get renamed or moved?"
        )

    declared: dict[str, set[str]] = defaultdict(set)
    found_any = False
    for node in ast.walk(target_fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_load_only = (isinstance(func, ast.Name) and func.id == "load_only") or (
            isinstance(func, ast.Attribute) and func.attr == "load_only"
        )
        if not is_load_only:
            continue
        for arg in node.args:
            if (
                isinstance(arg, ast.Attribute)
                and isinstance(arg.value, ast.Name)
                and arg.value.id in metas
            ):
                declared[arg.value.id].add(arg.attr)
                found_any = True
    if not found_any:
        raise SystemExit(
            "load_only_guard: no load_only(Model.column, ...) calls found in "
            "list_tasks_core. The compact load_only sets may have moved."
        )
    return declared


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
    metas = introspect_models()
    reads = collect_read_columns(helpers_path, metas)
    declared = collect_declared_columns(tasks_query_path, metas)
    return compute_violations(reads, declared, metas)


# Which ``load_only`` set each model maps to, for actionable error messages.
_SET_HINT = {
    "TrialModel": "the trials `load_only(TrialModel.*)` set",
    "TaskModel": "the `load_only(TaskModel.*)` set",
    "ExperimentModel": "the experiments `load_only(ExperimentModel.*)` set",
}


def main() -> int:
    violations = find_violations()
    if not violations:
        print(
            "load_only_guard: OK -- every FE-surfaced Trial/Task/Experiment "
            "column read on the compact /tasks path is covered by its "
            "load_only() set."
        )
        return 0

    print("load_only_guard: FAILED\n", file=sys.stderr)
    print(
        "The compact /tasks response builders read these columns, but they are\n"
        "missing from the matching load_only() set in list_tasks_core\n"
        "(oddish/core/endpoints/tasks_query.py). Under async SQLAlchemy each one\n"
        "would lazy-load outside the request greenlet and 500 every GET /tasks\n"
        "with MissingGreenlet (the 2026-06-24 incident).\n",
        file=sys.stderr,
    )
    for model_name in sorted(violations):
        hint = _SET_HINT.get(model_name, f"the {model_name} load_only set")
        for column in violations[model_name]:
            print(f"  - {model_name}.{column}  ->  add to {hint}", file=sys.stderr)
    print(
        "\nFix: add each column above to its load_only(...) set, OR stop reading\n"
        "it in the compact builders (build_compact_trial_response /\n"
        "build_task_status_response_compact / _build_task_status_response).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
