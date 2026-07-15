from oddish.evals.analyzer.taxonomy import (
    Capability,
    Category,
    Taxonomy,
    render_capabilities,
    taxonomy_fingerprint,
    taxonomy_from_snapshot,
    taxonomy_snapshot,
)


def _tax() -> Taxonomy:
    return Taxonomy(
        categories=(
            Category("verification", "Verification failures", "stops early", 0),
            Category("tool", "Tool failures", "wrong tool", 1),
        ),
        capabilities=(
            Capability(
                "agent-early-stop", "Agent Early Stop",
                "Stops early and assumes it's done.",
                "apex-swe: patches then solves without verifying.",
                primary_category="verification",
            ),
            Capability(
                "tool-selection-error", "Tool Selection Error",
                "Picks the familiar-but-wrong tool.",
                "APEX agents pick curl over MCP.",
                primary_category="tool",
                extra_categories=("verification",),
            ),
        ),
    )


def test_by_category_groups_on_primary_only():
    """extra_categories must NOT duplicate a capability into a second group --
    that is exactly the double-count is_primary exists to prevent."""
    groups = _tax().by_category()
    assert [c.slug for c, _ in groups] == ["verification", "tool"]
    assert [x.slug for x in groups[0][1]] == ["agent-early-stop"]
    assert [x.slug for x in groups[1][1]] == ["tool-selection-error"]


def test_by_category_orders_by_sort_order():
    tax = Taxonomy(
        categories=(Category("b", "B", "", 5), Category("a", "A", "", 1)),
        capabilities=(),
    )
    assert [c.slug for c, _ in tax.by_category()] == ["a", "b"]


def test_render_capabilities_includes_slug_name_description_example():
    out = render_capabilities(_tax())
    assert "verification — Verification failures" in out
    assert "agent-early-stop" in out
    assert "Stops early and assumes it's done." in out
    assert "apex-swe: patches then solves without verifying." in out


def test_render_capabilities_shows_extra_categories_as_cross_reference():
    out = render_capabilities(_tax())
    assert "also: verification" in out


def test_fingerprint_is_stable_and_content_sensitive():
    a = taxonomy_fingerprint(_tax())
    assert a == taxonomy_fingerprint(_tax())
    assert len(a) == 12
    changed = Taxonomy(
        categories=_tax().categories,
        capabilities=_tax().capabilities[:1],
    )
    assert taxonomy_fingerprint(changed) != a


def test_fingerprint_unchanged_by_row_reorder():
    """DB queries don't guarantee row order; the fingerprint must depend on
    taxonomy content, not on the order rows came back in."""
    tax = _tax()
    reordered = Taxonomy(
        categories=tuple(reversed(tax.categories)),
        capabilities=tuple(reversed(tax.capabilities)),
    )
    assert taxonomy_fingerprint(tax) == taxonomy_fingerprint(reordered)


def test_snapshot_round_trips():
    tax = _tax()
    assert taxonomy_from_snapshot(taxonomy_snapshot(tax)) == tax
