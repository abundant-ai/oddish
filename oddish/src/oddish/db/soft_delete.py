"""Soft-delete infrastructure for ORM models.

Models that share :class:`oddish.db.models.TimestampedMixin` already have
a ``deleted_at TIMESTAMPTZ`` column. This module turns that column into
real soft-delete semantics:

* SELECT/UPDATE/DELETE statements emitted via SQLAlchemy ORM automatically
  pick up a ``WHERE deleted_at IS NULL`` clause for every soft-deletable
  entity in the FROM list, including alias targets and eager-loaded
  relationships (``selectinload`` / ``joinedload`` / lazy load).
* Callers that need to read or rewrite tombstoned rows (admin tooling,
  restore flows) opt out per-statement via
  ``.execution_options(include_deleted=True)``.

The filter is registered against the SQLAlchemy ``Session`` class so it
applies to every async session created from the shared session maker
(both the oddish standalone server and the backend Modal app share that
same maker). Both packages publish their soft-deletable models through
:func:`register_soft_delete_models`.

Raw ``text()`` SQL is *not* covered by this listener -- the dispatcher
claim path, cleanup sweep, and admin diagnostics each add the filter
inline because they bypass the ORM.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria
from sqlalchemy.orm.session import ORMExecuteState


__all__ = [
    "INCLUDE_DELETED_OPTION",
    "get_soft_delete_models",
    "install_soft_delete_filter",
    "is_soft_delete_filter_installed",
    "register_soft_delete_models",
]


# Execution-option key callers set to opt OUT of the auto-filter.
# Example: ``session.execute(stmt.execution_options(include_deleted=True))``.
INCLUDE_DELETED_OPTION = "include_deleted"


# Module-level registry of mapped classes that have a ``deleted_at`` column
# and should be auto-filtered. Populated by ``register_soft_delete_models``.
# Held as a list (not a set) so insertion order is stable for tests/debug.
_SOFT_DELETE_MODELS: list[type[Any]] = []
_INSTALLED: bool = False


def register_soft_delete_models(*models: type[Any]) -> None:
    """Add ORM models to the soft-delete auto-filter registry.

    Idempotent: re-registering the same class is a no-op. Safe to call
    from multiple packages (oddish core registers its models; backend
    registers its auth models on top).

    Each registered class must expose a ``deleted_at`` mapped column.
    """
    for model in models:
        if model in _SOFT_DELETE_MODELS:
            continue
        if not hasattr(model, "deleted_at"):
            raise TypeError(
                f"register_soft_delete_models: {model!r} has no 'deleted_at' column"
            )
        _SOFT_DELETE_MODELS.append(model)


def get_soft_delete_models() -> tuple[type[Any], ...]:
    """Return the currently registered soft-deletable model classes."""
    return tuple(_SOFT_DELETE_MODELS)


def _apply_filter(execute_state: ORMExecuteState) -> None:
    """Session-level hook that injects the soft-delete WHERE clause.

    Fires for every ORM-issued SELECT / UPDATE / DELETE. We skip the
    statement entirely when the caller passed ``include_deleted=True``,
    when there are no registered models (early import order), or when
    the statement isn't an ORM-mapped read/write (e.g. raw ``text()``,
    which has ``is_select == False`` and an empty bind mapper).
    """
    if execute_state.execution_options.get(INCLUDE_DELETED_OPTION, False):
        return
    if not _SOFT_DELETE_MODELS:
        return
    if not (
        execute_state.is_select or execute_state.is_update or execute_state.is_delete
    ):
        return

    # ``include_aliases=True`` makes the criteria attach to aliased
    # selectables too, so it follows eager loads, sub-selects, and
    # ``aliased(TaskModel)`` joins without extra wiring.
    execute_state.statement = execute_state.statement.options(
        *(
            with_loader_criteria(
                model,
                lambda cls: cls.deleted_at.is_(None),
                include_aliases=True,
            )
            for model in _SOFT_DELETE_MODELS
        )
    )


def install_soft_delete_filter() -> None:
    """Install the ``do_orm_execute`` listener exactly once.

    Called from :mod:`oddish.db.connection` at import time so every
    session created by the shared session maker inherits the filter.
    Safe to call multiple times -- subsequent calls are no-ops.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    event.listen(Session, "do_orm_execute", _apply_filter)
    _INSTALLED = True


def is_soft_delete_filter_installed() -> bool:
    """Return True once :func:`install_soft_delete_filter` has run."""
    return _INSTALLED
