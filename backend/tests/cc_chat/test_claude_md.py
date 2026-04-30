from api.services.cc_chat.claude_md import render_claude_md


def test_render_claude_md_includes_experiment_id_and_layout():
    md = render_claude_md(
        experiment_id="exp-123",
        trial_ids=["trial-a", "trial-b"],
    )
    # Header tells the agent who it is
    assert "exp-123" in md
    # Layout description so the agent knows how to navigate
    assert "result.json" in md
    assert "trajectory.json" in md
    assert "verifier" in md
    # Lists the trials it can drill into
    assert "trial-a" in md
    assert "trial-b" in md
    # Steers the agent away from cat-ing every log
    assert "Glob" in md or "Grep" in md
    # No template placeholders left behind
    assert "{" not in md and "}" not in md


def test_render_claude_md_handles_empty_experiment():
    md = render_claude_md(experiment_id="exp-empty", trial_ids=[])
    assert "exp-empty" in md
    # Don't fail; tell the agent there's nothing yet
    assert "no trial" in md.lower()
