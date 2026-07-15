"""The section briefs are the analyzer taxonomy. reduce.txt composes all four;
the sandbox path takes a per-bucket subset. Both read the same fragments."""

from oddish.evals.analyzer.prompt_builder import (
    SECTION_KEYS,
    SECTION_KEYS_BY_BUCKET,
    build_reduce_prompt,
    section_brief,
    sections_block,
)
from oddish.evals.analyzer.taxonomy import Taxonomy

# The exact bytes reduce.txt carried before the split. build_reduce_prompt must
# keep emitting these verbatim -- the live API path depends on this prose.
_ORIGINAL_BLOCK = (
    "- bad_failure_content: reward hacking, worked backwards from the oracle. Organize\n"
    "  by 1a (task ambiguity/specification) and 1b (task security/construction).\n"
    "- good_failure_content: genuine model-capability failures — what the model failed at.\n"
    "- universal_capabilities_content: organize the good failures by failure\n"
    "  category, then by capability within each category. Use one `## <Category\n"
    "  name>` heading per category that has findings, in the order the rubric listed\n"
    "  them, and one `### <Capability name>` subheading per capability with findings\n"
    "  in that category. Cite every claim with the finding's trajectory_link\n"
    "  verbatim. If a capability carries cross-reference categories, note them\n"
    "  inline (\"also: long-horizon\") rather than repeating the capability under a\n"
    "  second heading. Group findings whose capability_slug was newly proposed\n"
    "  (not in the rubric) under a final `## Proposed capabilities` heading.\n"
    "- headroom_analysis: based on the good failures, where is the most capability headroom?"
)


def test_section_keys_order_matches_reduce_txt():
    assert SECTION_KEYS == (
        "bad_failure_content",
        "good_failure_content",
        "universal_capabilities_content",
        "headroom_analysis",
    )


def test_sections_block_reassembles_the_original_bytes():
    assert sections_block(SECTION_KEYS) == _ORIGINAL_BLOCK


def test_build_reduce_prompt_still_contains_every_brief():
    prompt = build_reduce_prompt([], {"trials": 0, "bad": 0, "good": 0}, Taxonomy())
    assert _ORIGINAL_BLOCK in prompt


def test_bucket_split_covers_every_key_exactly_once():
    """reduce.txt derives 1 section from bad and 3 from good; no key is orphaned
    or double-owned."""
    bad = SECTION_KEYS_BY_BUCKET["bad"]
    good = SECTION_KEYS_BY_BUCKET["good"]
    assert bad == ("bad_failure_content",)
    assert good == (
        "good_failure_content",
        "universal_capabilities_content",
        "headroom_analysis",
    )
    assert sorted(bad + good) == sorted(SECTION_KEYS)


def test_section_brief_reads_the_fragment():
    assert section_brief("headroom_analysis").startswith("- headroom_analysis:")


def test_every_section_key_has_a_fragment():
    for key in SECTION_KEYS:
        assert section_brief(key).strip()


def test_universal_capabilities_organizes_by_category_then_capability():
    brief = section_brief("universal_capabilities_content")
    # Must mention category and capability organization explicitly.
    assert "category" in brief.lower()
    assert "capability" in brief.lower()
    # Must use markdown heading patterns for category and capability sections.
    assert "## <Category" in brief
    assert "### <Capability" in brief
    # Must cite findings with trajectory_link.
    assert "trajectory_link" in brief
    # 3a/3b/3c is no longer the good-bucket frame.
    assert "3a" not in brief


def test_universal_capabilities_mentions_proposed_bucket():
    """Findings citing a not-yet-promoted slug resolve to no live capability;
    they must land somewhere visible rather than vanish."""
    brief = section_brief("universal_capabilities_content")
    # Must appear as a markdown heading, not just a substring.
    assert "## Proposed capabilities" in brief
