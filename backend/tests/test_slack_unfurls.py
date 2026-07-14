from __future__ import annotations

import hashlib
import hmac

from api.routers.slack import verify_slack_signature
from api.services.slack_unfurls import (
    Summary,
    TrialSnapshot,
    outcome_glyph,
    parse_oddish_link,
    render_blocks,
    trial_outcome,
)


def _trial(
    *,
    task_id: str = "task-1",
    task_name: str = "checkout",
    agent: str = "codex",
    model: str | None = "openai/gpt-5",
    status: str = "success",
    reward: float | None = 1.0,
    error_message: str | None = None,
) -> TrialSnapshot:
    return TrialSnapshot(
        task_id=task_id,
        task_name=task_name,
        agent=agent,
        model=model,
        status=status,
        reward=reward,
        error_message=error_message,
        input_tokens=None,
        cache_tokens=None,
        cache_write_tokens=None,
        output_tokens=None,
        cost_usd=1.25,
    )


def test_parse_oddish_links_are_origin_and_route_scoped():
    dashboard = "https://www.oddish.app"
    task = parse_oddish_link("https://www.oddish.app/tasks/task-1", dashboard)
    assert task is not None
    assert (task.kind, task.identifier, task.public) == ("task", "task-1", False)

    experiment = parse_oddish_link(
        "https://www.oddish.app/experiments/team%252Fexperiment", dashboard
    )
    assert experiment is not None
    assert experiment.identifier == "team/experiment"

    shared = parse_oddish_link("https://www.oddish.app/share/token", dashboard)
    assert shared is not None and shared.public is True
    assert parse_oddish_link("https://evil.example/tasks/task-1", dashboard) is None
    assert (
        parse_oddish_link("https://www.oddish.app/tasks/task-1/logs", dashboard) is None
    )


def test_outcome_symbols_match_oddish_matrix_semantics():
    assert outcome_glyph(_trial()) == "✓"
    assert outcome_glyph(_trial(reward=0.5)) == ".50"
    assert outcome_glyph(_trial(reward=0.0)) == "✗"
    assert outcome_glyph(_trial(status="success", reward=None)) == "–"
    assert outcome_glyph(_trial(status="queued", reward=None)) == "⟳"
    assert outcome_glyph(_trial(status="running", reward=None)) == "⟳"
    assert outcome_glyph(_trial(status="retrying", reward=None)) == "◌"
    assert outcome_glyph(_trial(status="skipped", error_message="gated")) == "⊘"
    assert trial_outcome(_trial(status="failed", error_message="boom")) == "error"

    timeout_with_reward = _trial(
        status="failed",
        reward=0.5,
        error_message="AgentTimeoutError: time limit",
    )
    assert trial_outcome(timeout_with_reward) == "partial"


def test_task_card_uses_glyph_row_and_collapses_after_limit():
    small = Summary(
        "task",
        "checkout",
        "https://www.oddish.app/tasks/task-1",
        (_trial(), _trial(reward=0.5), _trial(reward=0.0)),
        1,
        4,
    )
    blocks = render_blocks(small)
    assert blocks[0]["text"]["text"] == "checkout · v4"
    assert blocks[1]["text"]["text"] == "✓  .50  ✗"

    large = Summary(
        "task",
        "checkout",
        small.url,
        tuple(_trial() for _ in range(13)),
        1,
        4,
    )
    large_blocks = render_blocks(large)
    assert not any(
        block.get("text", {}).get("text", "").startswith("✓  ✓")
        for block in large_blocks
    )
    results = large_blocks[1]["fields"][0]["text"]
    assert "✓ 13" in results


def test_small_experiment_gets_matrix_but_large_one_does_not():
    trials = (
        _trial(task_id="a", task_name="checkout", agent="codex"),
        _trial(task_id="a", task_name="checkout", agent="claude", reward=0.0),
        _trial(task_id="b", task_name="tax", agent="codex", reward=0.5),
        _trial(task_id="b", task_name="tax", agent="claude"),
    )
    small = Summary(
        "experiment",
        "regression",
        "https://www.oddish.app/experiments/exp-1",
        trials,
        2,
    )
    small_blocks = render_blocks(small)
    matrix = small_blocks[1]["text"]["text"]
    assert matrix.startswith("```Task")
    assert "checkout" in matrix and "tax" in matrix
    assert "✓" in matrix and "✗" in matrix and ".50" in matrix

    large_trials = tuple(
        _trial(task_id=str(i), task_name=f"task-{i}") for i in range(9)
    )
    large = Summary("experiment", "large", small.url, large_trials, 9)
    assert not any(
        block.get("text", {}).get("text", "").startswith("```")
        for block in render_blocks(large)
    )


def test_experiment_score_includes_non_successful_trials_with_rewards():
    summary = Summary(
        "experiment",
        "timeouts",
        "https://www.oddish.app/experiments/exp-1",
        (
            _trial(
                task_id="a",
                status="failed",
                reward=0.5,
                error_message="AgentTimeoutError: time limit",
            ),
            _trial(task_id="b", status="failed", reward=1.0),
        ),
        2,
    )

    fields = next(
        block["fields"] for block in render_blocks(summary) if "fields" in block
    )
    assert fields[2]["text"] == "*Avg score*\n75.0%"


def test_sampled_summary_labels_bounded_trial_window():
    summary = Summary(
        "experiment",
        "large",
        "https://www.oddish.app/experiments/exp-1",
        (_trial(), _trial()),
        100,
        total_trials=5000,
    )

    fields = next(
        block["fields"] for block in render_blocks(summary) if "fields" in block
    )
    assert fields[0]["text"].startswith("*Results (latest 2)*")
    assert fields[1]["text"] == "*Completion (latest 2)*\n2/2 sampled · 5000 total"
    assert fields[2]["text"].startswith("*Avg score (latest 2)*")
    assert fields[3]["text"].startswith("*Cost (latest 2)*")


def test_slack_signature_verification_checks_hmac_and_age():
    body = b'{"type":"event_callback"}'
    timestamp = "1000"
    secret = "signing-secret"
    base = b"v0:" + timestamp.encode() + b":" + body
    signature = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()

    assert verify_slack_signature(
        body,
        timestamp=timestamp,
        signature=signature,
        signing_secret=secret,
        now=1001,
    )
    assert not verify_slack_signature(
        body + b"x",
        timestamp=timestamp,
        signature=signature,
        signing_secret=secret,
        now=1001,
    )
    assert not verify_slack_signature(
        body,
        timestamp=timestamp,
        signature=signature,
        signing_secret=secret,
        now=1400,
    )
