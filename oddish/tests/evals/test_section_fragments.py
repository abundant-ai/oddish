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

# The exact bytes reduce.txt carried before the split, for the three briefs that
# have not been rewritten since. build_reduce_prompt must keep emitting these
# verbatim -- the live API path depends on this prose. headroom_analysis is
# deliberately excluded: it was rewritten to also ask for scaling suggestions,
# so pinning its old bytes would pin the bug this section exists to fix.
#
# Updated for per-model reduce wiring: each of the three briefs below gained a
# trailing "cover only what holds across models" line, so the pin now includes it.
_ORIGINAL_BLOCK = (
    "- bad_failure_content: reward hacking, worked backwards from the oracle. Organize\n"
    "  by 1a (task ambiguity/specification) and 1b (task security/construction).\n"
    "Cover only what holds across models; put single-model observations in that model's by_model entry.\n"
    "- good_failure_content: genuine model-capability failures — what the model failed at.\n"
    "Cover only what holds across models; put single-model observations in that model's by_model entry.\n"
    "- universal_capabilities_content: organize the good failures by failure\n"
    "  category, then by capability within each category. Use one `## <Category\n"
    "  name>` heading per category that has findings, in the order the rubric listed\n"
    "  them, and one `### <Capability name>` subheading per capability with findings\n"
    "  in that category. Cite every claim with the finding's trajectory_link\n"
    "  verbatim. If a capability carries cross-reference categories, note them\n"
    "  inline (\"also: long-horizon\") rather than repeating the capability under a\n"
    "  second heading. Group findings whose capability_slug was newly proposed\n"
    "  (not in the rubric) under a final `## Proposed capabilities` heading.\n"
    "  Cover only what holds across models; put single-model observations in that\n"
    "  model's by_model entry."
)


def test_section_keys_order_matches_reduce_txt():
    assert SECTION_KEYS == (
        "bad_failure_content",
        "good_failure_content",
        "universal_capabilities_content",
        "headroom_analysis",
    )


def test_sections_block_reassembles_the_original_bytes():
    assert sections_block(SECTION_KEYS[:3]) == _ORIGINAL_BLOCK


def test_build_reduce_prompt_still_contains_every_brief():
    prompt = build_reduce_prompt([], {"trials": 0, "bad": 0, "good": 0}, Taxonomy())
    assert _ORIGINAL_BLOCK in prompt
    assert section_brief("headroom_analysis") in prompt


def test_headroom_brief_asks_for_grounded_scaling_suggestions():
    """The section's whole purpose: suggestions specific enough to act on, which
    means naming a task rather than restating the taxonomy."""
    brief = section_brief("headroom_analysis")
    for heading in ("### New tasks to farm", "### Harder variants",
                    "### Where to spend effort"):
        assert heading in brief
    assert "task roster" in brief


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


def test_by_model_output_shape_names_every_required_key():
    from oddish.evals.analyzer.prompt_builder import by_model_output_shape

    shape = by_model_output_shape()
    for key in (
        "by_model", "cross_model_comparison", "model", "narrative",
        "relative_strengths", "relative_weaknesses", "distinctive_failures",
    ):
        assert key in shape


def test_denominators_block_renders_counts_per_model():
    from oddish.evals.analyzer.prompt_builder import denominators_block

    block = denominators_block({
        "claude-opus-4-8": {"trials": 10, "scored": 10, "solved": 6,
                            "mean_reward": 0.6, "analyzed": 4, "bad": 1, "good": 3},
    })
    assert "claude-opus-4-8" in block
    assert "10 trials" in block
    assert "6 solved" in block


def test_denominators_block_handles_unscored_models():
    from oddish.evals.analyzer.prompt_builder import denominators_block

    block = denominators_block({
        "claude-opus-4-8": {"trials": 2, "scored": 0, "solved": 0,
                            "mean_reward": None, "analyzed": 0, "bad": 0, "good": 0},
    })
    assert "mean reward n/a" in block


def test_denominators_block_empty_is_explicit():
    from oddish.evals.analyzer.prompt_builder import denominators_block

    assert denominators_block({}) == "(no per-model denominators available)"


def test_reduce_prompt_includes_denominators_and_by_model_shape():
    from oddish.evals.analyzer.prompt_builder import build_reduce_prompt

    prompt = build_reduce_prompt(
        [], {"trials": 1, "bad": 0, "good": 1}, Taxonomy(), {"t1": ["claude-opus-4-8"]},
        denominators={"claude-opus-4-8": {"trials": 1, "scored": 1, "solved": 0,
                                          "mean_reward": 0.0, "analyzed": 1,
                                          "bad": 0, "good": 1}},
    )
    assert "claude-opus-4-8" in prompt
    assert "cross_model_comparison" in prompt
