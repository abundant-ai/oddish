"""Allocation math for Bedrock actual-cost attribution.

The invariant that matters is conservation: every dollar Cost Explorer reports
must land on exactly one trial or be reported as unattributed. Silent loss here
would understate real spend.
"""

import pytest

from scripts.bedrock_cost_attribution import (
    TrialWeight,
    allocate,
    normalize_model_key,
    rollup,
    suggest_mapping,
)

JUL = "2026-07"
OPUS = "global.anthropic.claude-opus-4-8"
FABLE = "global.anthropic.claude-fable-5"


def _trial(tid, cost, model=OPUS, period=JUL, task="task-a", exp="exp-1"):
    return TrialWeight(
        trial_id=tid,
        period=period,
        model=model,
        list_cost_usd=cost,
        task_id=task,
        experiment_id=exp,
    )


def test_allocates_proportionally_to_list_cost():
    trials = [_trial("a", 30.0), _trial("b", 10.0)]
    result = allocate({(JUL, OPUS): 100.0}, trials)

    assert result.per_trial["a"] == pytest.approx(75.0)
    assert result.per_trial["b"] == pytest.approx(25.0)


def test_conserves_the_invoice_total():
    trials = [_trial(str(i), float(i + 1)) for i in range(17)]
    result = allocate({(JUL, OPUS): 1234.56}, trials)

    assert sum(result.per_trial.values()) == pytest.approx(1234.56)


def test_zero_weight_group_splits_evenly_rather_than_vanishing():
    """All-unpriced trials still have to absorb the invoice."""
    trials = [_trial("a", 0.0), _trial("b", 0.0), _trial("c", 0.0)]
    result = allocate({(JUL, OPUS): 90.0}, trials)

    assert result.per_trial == {"a": 30.0, "b": 30.0, "c": 30.0}
    assert sum(result.per_trial.values()) == pytest.approx(90.0)


def test_invoice_line_with_no_trials_is_reported_not_dropped():
    result = allocate({(JUL, FABLE): 500.0}, [_trial("a", 10.0, model=OPUS)])

    assert result.unattributed == {(JUL, FABLE): 500.0}
    assert result.per_trial["a"] == 0.0


def test_trials_with_no_invoice_line_are_reported_and_zeroed():
    """A model we ran but CE has no line for must not silently inherit cost."""
    trials = [_trial("a", 10.0, model=OPUS), _trial("b", 40.0, model=FABLE)]
    result = allocate({(JUL, OPUS): 20.0}, trials)

    assert result.per_trial["a"] == pytest.approx(20.0)
    assert result.per_trial["b"] == 0.0
    assert result.unreconciled == {(JUL, FABLE): 40.0}


def test_periods_and_models_do_not_bleed_into_each_other():
    trials = [
        _trial("jul-opus", 10.0, model=OPUS, period="2026-07"),
        _trial("aug-opus", 10.0, model=OPUS, period="2026-08"),
        _trial("jul-fable", 10.0, model=FABLE, period="2026-07"),
    ]
    result = allocate(
        {("2026-07", OPUS): 100.0, ("2026-08", OPUS): 7.0, ("2026-07", FABLE): 3.0},
        trials,
    )

    assert result.per_trial["jul-opus"] == pytest.approx(100.0)
    assert result.per_trial["aug-opus"] == pytest.approx(7.0)
    assert result.per_trial["jul-fable"] == pytest.approx(3.0)


def test_rollup_sums_by_task():
    trials = [
        _trial("a", 30.0, task="task-a"),
        _trial("b", 10.0, task="task-a"),
        _trial("c", 60.0, task="task-b"),
    ]
    result = allocate({(JUL, OPUS): 100.0}, trials)
    by_task = rollup(trials, result.per_trial, "task_id")

    assert by_task["task-a"]["actual_usd"] == pytest.approx(40.0)
    assert by_task["task-a"]["trials"] == 2
    assert by_task["task-b"]["actual_usd"] == pytest.approx(60.0)


class TestNormalizeModelKey:
    def test_collapses_ce_and_trials_spellings_onto_one_key(self):
        assert normalize_model_key("USE1-BedrockClaude3.5Sonnet-InputTokenCount") == (
            normalize_model_key("global.anthropic.claude-3-5-sonnet-v1")
        )

    def test_distinguishes_different_models(self):
        assert normalize_model_key("global.anthropic.claude-opus-4-8") != (
            normalize_model_key("global.anthropic.claude-fable-5")
        )

    def test_token_type_suffixes_share_a_key(self):
        """Input/output/cache lines for one model must fold together."""
        assert normalize_model_key("USE1-BedrockClaudeOpus4.8-InputTokenCount") == (
            normalize_model_key("USE1-BedrockClaudeOpus4.8-OutputTokenCount")
        )


class TestSuggestMapping:
    def test_matches_on_normalized_key(self):
        got = suggest_mapping(["USE1-BedrockClaudeOpus4.8-InputTokenCount"], [OPUS])
        assert got["USE1-BedrockClaudeOpus4.8-InputTokenCount"] == OPUS

    def test_refuses_to_guess_when_nothing_matches(self):
        """An unmapped entry is loud; a wrong one silently misattributes money."""
        got = suggest_mapping(["USE1-SomethingElse-InputTokenCount"], [OPUS])
        assert got["USE1-SomethingElse-InputTokenCount"] is None
