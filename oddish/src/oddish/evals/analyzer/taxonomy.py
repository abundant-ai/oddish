"""The capability taxonomy as pure data.

Loaded from Postgres by ``oddish.db.taxonomy_query`` and passed down; nothing
here touches a session. Keeping it pure is what lets ``prompt_builder`` render
the rubric without acquiring the I/O its docstring promises it does not do.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    slug: str
    name: str
    description: str = ""
    sort_order: int = 0


@dataclass(frozen=True)
class Capability:
    slug: str
    name: str
    description: str
    example: str = ""
    primary_category: str = ""
    # Non-primary tags. Deliberately excluded from by_category() grouping:
    # counting a capability under every tag would make category totals exceed
    # num_good_failures. These render as cross-references instead.
    extra_categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class Taxonomy:
    categories: tuple[Category, ...] = ()
    capabilities: tuple[Capability, ...] = ()

    def by_category(self) -> list[tuple[Category, list[Capability]]]:
        ordered = sorted(self.categories, key=lambda c: (c.sort_order, c.slug))
        return [
            (cat, [c for c in self.capabilities if c.primary_category == cat.slug])
            for cat in ordered
        ]


def render_capabilities(taxonomy: Taxonomy) -> str:
    lines: list[str] = []
    for cat, caps in taxonomy.by_category():
        if not caps:
            continue
        lines.append(f"### {cat.slug} — {cat.name}")
        for c in caps:
            extra = (
                f"   (also: {', '.join(c.extra_categories)})"
                if c.extra_categories else ""
            )
            lines.append(f"  {c.slug} — {c.name}{extra}")
            lines.append(f"      {c.description}")
            if c.example:
                lines.append(f"      e.g. {c.example}")
        lines.append("")
    return "\n".join(lines).rstrip("\n")


def taxonomy_snapshot(taxonomy: Taxonomy) -> dict:
    return {
        "categories": [
            {"slug": c.slug, "name": c.name, "description": c.description,
             "sort_order": c.sort_order}
            for c in taxonomy.categories
        ],
        "capabilities": [
            {"slug": c.slug, "name": c.name, "description": c.description,
             "example": c.example, "primary_category": c.primary_category,
             "extra_categories": list(c.extra_categories)}
            for c in taxonomy.capabilities
        ],
    }


def taxonomy_from_snapshot(d: dict) -> Taxonomy:
    return Taxonomy(
        categories=tuple(Category(**c) for c in d.get("categories", [])),
        capabilities=tuple(
            Capability(
                slug=c["slug"], name=c["name"], description=c["description"],
                example=c.get("example", ""),
                primary_category=c.get("primary_category", ""),
                extra_categories=tuple(c.get("extra_categories", [])),
            )
            for c in d.get("capabilities", [])
        ),
    )


def taxonomy_fingerprint(taxonomy: Taxonomy) -> str:
    """Short content hash, stored as ``analyzers.taxonomy_version``.

    json.dumps(sort_keys=True) only sorts dict keys, not list elements, so the
    snapshot's lists are sorted by slug here to make the fingerprint
    order-independent -- a row reshuffle from the DB should not read as a
    taxonomy change.
    """
    snap = taxonomy_snapshot(taxonomy)
    snap["categories"].sort(key=lambda c: c["slug"])
    snap["capabilities"].sort(key=lambda c: c["slug"])
    blob = json.dumps(snap, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:12]
