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
from sqlalchemy.sql.elements import TextClause

from .naming import normalize_tag_key, normalize_tag_value


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


def _split_token(token: str) -> tuple[str, str | None]:
    """Split a filter token on the FIRST ':' into (normalized_key, normalized_value).

    Derived tags (``core/tags/derived.py``) all share one ``normalized_key``
    per category (e.g. "category") with the discriminator in
    ``normalized_value`` — the dashboard picker emits tokens like
    ``category:reverse-engineering`` (``tag-filter-dropdown.tsx``) to name
    one of them. ``value`` is ``None`` for a bare token (no ':').
    """
    if ":" in token:
        key_part, _, value_part = token.partition(":")
        return normalize_tag_key(key_part), normalize_tag_value(value_part)
    return normalize_tag_key(token), None


async def resolve_names_to_ids(
    session,
    *,
    org_id: str | None,
    ast: TagFilterAST,
) -> tuple[ResolvedTagFilter, set[str]]:
    """Resolve every filter token in the AST to tag ids.

    A token is one of:
      * a tag id (what the dashboard picker and saved filters send) —
        matches ``tags.id`` exactly.
      * a ``key:value`` name (e.g. ``category:reverse-engineering``) —
        matches the one tag with that exact ``(normalized_key,
        normalized_value)`` pair. This is how derived tags are addressed,
        since many of them share a ``normalized_key``.
      * a bare key name (CLI / API callers, or a legacy value-less tag) —
        matches EVERY tag with that ``normalized_key``. For a value-less
        tag that is still exactly one row, so this is unchanged from
        before; for a key that now fans out into several values (every
        derived category), "match everything under this key" is the only
        reading that doesn't silently pick one arbitrary row (the old
        flat-dict lookup did, last-row-wins).

    Follows ``merged_into_id`` so aliases resolve to the survivor. Drops
    DELETED tag rows. Returns (resolved, unknown_tokens) — a token is
    unknown iff it resolved to zero tag ids.
    """
    raw_tokens = {t for t in (*ast.all, *ast.any_, *ast.none) if t}
    if not raw_tokens:
        return ResolvedTagFilter(all_ids=[], any_ids=[], none_ids=[]), set()
    wanted_names = {_split_token(t)[0] for t in raw_tokens if _split_token(t)[0]}

    rows = (
        await session.execute(
            text(
                """
                SELECT t.id,
                       t.normalized_key,
                       t.normalized_value,
                       COALESCE(t.merged_into_id, t.id) AS resolved_id
                FROM tags t
                WHERE t.deleted_at IS NULL
                  AND t.state <> 'DELETED'
                  AND COALESCE(t.org_id, '') = COALESCE(CAST(:org_id AS TEXT), '')
                  AND (t.id = ANY(:ids) OR t.normalized_key = ANY(:names))
                """
            ),
            {
                "org_id": org_id,
                "ids": list(raw_tokens),
                "names": list(wanted_names),
            },
        )
    ).all()
    by_id: dict[str, str] = {}
    by_key: dict[str, list[str]] = {}
    by_key_value: dict[tuple[str, str], str] = {}
    for tag_id, normalized_key, normalized_value, resolved_id in rows:
        rid = str(resolved_id)
        by_id[str(tag_id)] = rid
        by_key.setdefault(str(normalized_key), []).append(rid)
        if normalized_value is not None:
            by_key_value[(str(normalized_key), str(normalized_value))] = rid

    def _lookup_all(token: str) -> list[str]:
        rid = by_id.get(token)
        if rid is not None:
            return [rid]
        key, value = _split_token(token)
        if value is not None:
            rid = by_key_value.get((key, value))
            return [rid] if rid is not None else []
        return list(by_key.get(key, ()))

    def _convert(tokens: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for t in tokens:
            if not t:
                continue
            for rid in _lookup_all(t):
                if rid not in seen:
                    seen.add(rid)
                    out.append(rid)
        return out

    resolved = ResolvedTagFilter(
        all_ids=_convert(ast.all),
        any_ids=_convert(ast.any_),
        none_ids=_convert(ast.none),
    )
    unknown = {t for t in raw_tokens if not _lookup_all(t)}
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
    predicates: list[TextClause] = []
    if resolved.all_ids:
        predicates.append(
            text("tasks.effective_tag_ids @> CAST(:tags_all_ids AS TEXT[])").bindparams(
                tags_all_ids=list(resolved.all_ids)
            )
        )
    if resolved.any_ids:
        predicates.append(
            text("tasks.effective_tag_ids && CAST(:tags_any_ids AS TEXT[])").bindparams(
                tags_any_ids=list(resolved.any_ids)
            )
        )
    if resolved.none_ids:
        predicates.append(
            text(
                "NOT (tasks.effective_tag_ids && CAST(:tags_none_ids AS TEXT[]))"
            ).bindparams(tags_none_ids=list(resolved.none_ids))
        )
    return predicates
