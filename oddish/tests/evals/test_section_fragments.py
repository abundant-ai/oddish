"""The section briefs are the analyzer taxonomy. reduce.txt composes all four;
the sandbox path takes a per-bucket subset. Both read the same fragments."""

from oddish.evals.analyzer.prompt_builder import (
    SECTION_KEYS,
    SECTION_KEYS_BY_BUCKET,
    build_reduce_prompt,
    section_brief,
    sections_block,
)

# The exact bytes reduce.txt carried before the split, for the three briefs that
# have not been rewritten since. build_reduce_prompt must keep emitting these
# verbatim -- the live API path depends on this prose. headroom_analysis is
# deliberately excluded: it was rewritten to also ask for scaling suggestions,
# so pinning its old bytes would pin the bug this section exists to fix.
_ORIGINAL_BLOCK = (
    "- bad_failure_content: reward hacking, worked backwards from the oracle. Organize\n"
    "  by 1a (task ambiguity/specification) and 1b (task security/construction).\n"
    "- good_failure_content: genuine model-capability failures — what the model failed at.\n"
    "- universal_capabilities_content: organize the good failures under 3a problem\n"
    "  identification, 3b implementation, 3c syntax, and any emergent capability categories\n"
    "  the findings surfaced."
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
    prompt = build_reduce_prompt([], {"trials": 0, "bad": 0, "good": 0})
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
