"""Filter AST + name resolution for the task browser.

The browse query exposes three sets — ``all`` (AND), ``any`` (OR), and
``none`` (NOT) — over ``tasks.effective_tag_ids``. Users provide either
tag IDs or human names; this module reuses the **shared tag normalizer**
(``oddish.core.tag_naming.normalize_tag_key``) so the same
whitespace, punctuation, and NFKC rules apply to both filter inputs and
``tags.normalized_key`` — otherwise a filter like ``--tag "Flaky Trial"``
would fail to resolve a tag created with the same display name.

Saved filters persist stable IDs; aliases are resolved at read by
following ``merged_into_id``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.sql.elements import ColumnElement

from oddish.core.tag_naming import normalize_tag_key


def _normalize_each(values: list[str]) -> list[str]:
    """Apply the shared tag normalizer; drop empties."""
    out: list[str] = []
    for raw in values:
        if not raw:
            continue
        n = normalize_tag_key(raw)
        if n:
            out.append(n)
    return out


@dataclass
class TagFilterAST:
    all: list[str] = field(default_factory=list)
    any_: list[str] = field(default_factory=list)
    none: list[str] = field(default_factory=list)

    @property
    def normalized_all(self) -> list[str]:
        return _normalize_each(self.all)

    @property
    def normalized_any(self) -> list[str]:
        return _normalize_each(self.any_)

    @property
    def normalized_none(self) -> list[str]:
        return _normalize_each(self.none)

    def is_empty(self) -> bool:
        return not (self.all or self.any_ or self.none)


@dataclass
class ResolvedTagFilter:
    all_ids: list[str]
    any_ids: list[str]
    none_ids: list[str]

    def is_empty(self) -> bool:
        return not (self.all_ids or self.any_ids or self.none_ids)


async def resolve_names_to_ids(
    session,
    *,
    org_id: str | None,
    ast: TagFilterAST,
) -> tuple[ResolvedTagFilter, set[str]]:
    """Look up ``normalized_key`` → ``id`` for every name in the AST.

    Follows ``merged_into_id`` so aliases resolve to the survivor. Drops
    DELETED tag rows. Returns (resolved, unknown_names).
    """
    wanted = set(ast.normalized_all) | set(ast.normalized_any) | set(ast.normalized_none)
    if not wanted:
        return ResolvedTagFilter(all_ids=[], any_ids=[], none_ids=[]), set()

    rows = (
        await session.execute(
            text(
                """
                SELECT t.normalized_key,
                       COALESCE(t.merged_into_id, t.id) AS resolved_id
                FROM tags t
                WHERE t.deleted_at IS NULL
                  AND t.state <> 'DELETED'
                  AND COALESCE(t.org_id, '') = COALESCE(CAST(:org_id AS TEXT), '')
                  AND t.normalized_key = ANY(:names)
                """
            ),
            {"org_id": org_id, "names": list(wanted)},
        )
    ).all()
    name_to_id: dict[str, str] = {}
    for normalized_key, resolved_id in rows:
        name_to_id[str(normalized_key)] = str(resolved_id)

    def _convert(names: list[str]) -> list[str]:
        return [name_to_id[n] for n in names if n in name_to_id]

    resolved = ResolvedTagFilter(
        all_ids=_convert(ast.normalized_all),
        any_ids=_convert(ast.normalized_any),
        none_ids=_convert(ast.normalized_none),
    )
    unknown = wanted - set(name_to_id)
    return resolved, unknown


def build_filter_predicates(resolved: ResolvedTagFilter):
    """Return a list of SQLAlchemy text predicates the caller appends to
    a ``WHERE`` clause that already references ``tasks.effective_tag_ids``.

    * AND  -> ``effective_tag_ids @> ARRAY['a','b']``
    * OR   -> ``effective_tag_ids && ARRAY['c','d']``
    * NOT  -> ``NOT (effective_tag_ids && ARRAY['e','f'])``

    All three ride the GIN(array_ops) index. The caller is responsible
    for binding parameters via ``.params(...)``.
    """
    predicates: list[ColumnElement] = []
    if resolved.all_ids:
        predicates.append(
            text(
                "tasks.effective_tag_ids @> CAST(:tags_all_ids AS TEXT[])"
            ).bindparams(tags_all_ids=list(resolved.all_ids))
        )
    if resolved.any_ids:
        predicates.append(
            text(
                "tasks.effective_tag_ids && CAST(:tags_any_ids AS TEXT[])"
            ).bindparams(tags_any_ids=list(resolved.any_ids))
        )
    if resolved.none_ids:
        predicates.append(
            text(
                "NOT (tasks.effective_tag_ids && CAST(:tags_none_ids AS TEXT[]))"
            ).bindparams(tags_none_ids=list(resolved.none_ids))
        )
    return predicates
