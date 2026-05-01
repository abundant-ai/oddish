from oddish.core.agent_identity import compute_agent_equivalence_key


def test_compute_agent_equivalence_key_uses_harness_model_provider() -> None:
    assert (
        compute_agent_equivalence_key("harbor", "openai/gpt-5.2", "openai")
        == "0da89390e2a686783f1bbeb84212b11930920fa25f77a997b7371ae365a1c41b"
    )


def test_compute_agent_equivalence_key_distinguishes_each_axis() -> None:
    base = compute_agent_equivalence_key("harbor", "openai/gpt-5.2", "openai")

    assert compute_agent_equivalence_key("other", "openai/gpt-5.2", "openai") != base
    assert compute_agent_equivalence_key("harbor", "openai/gpt-5.3", "openai") != base
    assert (
        compute_agent_equivalence_key("harbor", "openai/gpt-5.2", "azure") != base
    )
