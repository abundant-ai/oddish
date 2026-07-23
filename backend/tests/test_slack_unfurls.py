from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time

from fastapi import BackgroundTasks, Request

from api.routers import slack as slack_router
from api.routers.slack import slack_events, verify_slack_signature
from api.services.slack_unfurls import (
    SlackUnfurlConfig,
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
    assert outcome_glyph(_trial()) == ":white_check_mark:"
    assert outcome_glyph(_trial(reward=0.5)) == ".50"
    assert outcome_glyph(_trial(reward=0.0)) == ":x:"
    assert outcome_glyph(_trial(status="success", reward=None)) == ":heavy_minus_sign:"
    assert outcome_glyph(_trial(status="queued", reward=None)) == ":clock3:"
    assert (
        outcome_glyph(_trial(status="running", reward=None))
        == ":arrows_counterclockwise:"
    )
    assert outcome_glyph(_trial(status="retrying", reward=None)) == ":white_circle:"
    assert (
        outcome_glyph(_trial(status="skipped", error_message="gated"))
        == ":no_entry_sign:"
    )
    failed = _trial(status="failed", error_message="boom")
    assert trial_outcome(failed) == "error"
    assert outcome_glyph(failed) == ":warning:"

    timeout_with_reward = _trial(
        status="failed",
        reward=0.5,
        error_message="AgentTimeoutError: time limit",
    )
    assert trial_outcome(timeout_with_reward) == "partial"


def test_task_card_groups_trials_by_agent_and_collapses_after_limit():
    small = Summary(
        "task",
        "checkout",
        "https://www.oddish.app/tasks/task-1",
        (
            _trial(),
            _trial(reward=0.5),
            _trial(agent="claude", model="anthropic/claude-opus", reward=0.0),
        ),
        1,
        4,
    )
    blocks = render_blocks(small)
    assert blocks[0]["text"]["text"] == "checkout · v4"
    assert blocks[1]["fields"] == [
        {
            "type": "mrkdwn",
            "text": ("*codex · openai/gpt-5*\n:white_check_mark: .50"),
        },
        {
            "type": "mrkdwn",
            "text": "*claude · anthropic/claude-opus*\n:x:",
        },
    ]

    large = Summary(
        "task",
        "checkout",
        small.url,
        tuple(_trial() for _ in range(49)),
        1,
        4,
    )
    large_blocks = render_blocks(large)
    assert not any("fields" in block for block in large_blocks)
    results = large_blocks[1]["elements"][0]["text"]
    assert ":white_check_mark: 49 pass" in results


def test_small_experiment_gets_compact_task_rows_but_large_one_does_not():
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
    rows = [block["text"]["text"] for block in small_blocks[1:3]]
    assert rows == [
        (
            "*checkout*\n"
            "`codex · openai/gpt-5` :white_check_mark:   ·   "
            "`claude · openai/gpt-5` :x:"
        ),
        (
            "*tax*\n"
            "`codex · openai/gpt-5` .50   ·   "
            "`claude · openai/gpt-5` :white_check_mark:"
        ),
    ]

    large_trials = tuple(
        _trial(task_id=str(i), task_name=f"task-{i}") for i in range(9)
    )
    large = Summary("experiment", "large", small.url, large_trials, 9)
    assert not any(
        block.get("type") == "section" and "text" in block
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

    context = next(
        block["elements"]
        for block in render_blocks(summary)
        if block["type"] == "context"
    )
    assert "75.0% avg score" in context[1]["text"]


def test_sampled_summary_labels_bounded_trial_window():
    summary = Summary(
        "experiment",
        "large",
        "https://www.oddish.app/experiments/exp-1",
        (_trial(), _trial()),
        100,
        total_trials=5000,
    )

    context = next(
        block["elements"]
        for block in render_blocks(summary)
        if block["type"] == "context"
    )
    assert context[0]["text"].startswith("*Latest 2 · Results*")
    assert context[1]["text"] == (
        "100 tasks · latest 2: 2/2 sampled · 5000 total · 100.0% avg score · $2.50"
    )


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


def test_composer_link_event_is_enqueued_for_unfurl(monkeypatch):
    secret = "signing-secret"
    payload = {
        "type": "event_callback",
        "team_id": "T123",
        "event": {
            "type": "link_shared",
            "source": "composer",
            "unfurl_id": "U123",
            "links": [{"url": "https://www.oddish.app/tasks/task-1"}],
        },
    }
    body = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    signature = (
        "v0="
        + hmac.new(
            secret.encode(),
            b"v0:" + timestamp.encode() + b":" + body,
            hashlib.sha256,
        ).hexdigest()
    )
    config = SlackUnfurlConfig(
        enabled=True,
        signing_secret=secret,
        bot_token="xoxb-token",
        org_id="org-1",
        team_id="T123",
        allowed_channels=frozenset(),
        dashboard_url="https://www.oddish.app",
    )
    monkeypatch.setattr(slack_router, "load_slack_unfurl_config", lambda: config)

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "headers": [
                (b"x-slack-request-timestamp", timestamp.encode()),
                (b"x-slack-signature", signature.encode()),
            ],
        },
        receive,
    )
    background_tasks = BackgroundTasks()

    assert asyncio.run(slack_events(request, background_tasks)) == {"ok": True}
    assert len(background_tasks.tasks) == 1
    assert background_tasks.tasks[0].func is slack_router.process_link_shared_event
    assert background_tasks.tasks[0].args == (payload,)
