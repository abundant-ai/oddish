from oddish.core.agent_identity import compute_agent_equivalence_key


def test_equivalence_key_is_stable():
    a = compute_agent_equivalence_key("claude-code", "claude-sonnet-4-5", "anthropic")
    b = compute_agent_equivalence_key("claude-code", "claude-sonnet-4-5", "anthropic")
    assert a == b


def test_equivalence_key_normalizes_casing_and_whitespace():
    a = compute_agent_equivalence_key("claude-code", "claude-sonnet-4-5", "anthropic")
    b = compute_agent_equivalence_key(" Claude-Code ", "CLAUDE-SONNET-4-5", "Anthropic")
    assert a == b


def test_equivalence_key_differs_by_provider():
    anthropic = compute_agent_equivalence_key(
        "claude-code", "claude-sonnet-4-5", "anthropic"
    )
    bedrock = compute_agent_equivalence_key(
        "claude-code", "claude-sonnet-4-5", "bedrock"
    )
    assert anthropic != bedrock


def test_equivalence_key_differs_by_model():
    sonnet = compute_agent_equivalence_key(
        "claude-code", "claude-sonnet-4-5", "anthropic"
    )
    opus = compute_agent_equivalence_key(
        "claude-code", "claude-opus-4-5", "anthropic"
    )
    assert sonnet != opus


def test_equivalence_key_differs_by_harness():
    cc = compute_agent_equivalence_key("claude-code", "claude-sonnet-4-5", "anthropic")
    terminus = compute_agent_equivalence_key(
        "terminus-2", "claude-sonnet-4-5", "anthropic"
    )
    assert cc != terminus


def test_equivalence_key_handles_missing_model():
    """nop / oracle agents have no model; they still need a stable key."""
    a = compute_agent_equivalence_key("nop", None, "internal")
    b = compute_agent_equivalence_key("nop", None, "internal")
    assert a == b
    c = compute_agent_equivalence_key("nop", "", "internal")
    assert a == c
