"""Offline golden-set layer: every scenario in ``verdict_golden_set`` must
render a prompt and round-trip a synthetic judge reply through VerdictBlock's
parse path. This guards the two things a prompt/schema rewrite (like #1120's
``is_good`` -> ``verdict``) can silently break in CI -- rendering and parsing.
Whether the real model still returns the expected verdicts is the live
layer's job (``test_verdict_golden_set_live.py``)."""

from __future__ import annotations

import json

import pytest

from oddish.analyze.models import TaskVerdictModel
from verdict_golden_set import GOLDEN_CASES, build_block

_CASE_IDS = [c.name for c in GOLDEN_CASES]


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=_CASE_IDS)
def test_prompt_renders_the_scenario(case):
    prompt = build_block(case).build_prompt()

    # build_prompt() raising (not the sentinel) is the degraded path; getting
    # here means it rendered. Now pin that the scenario's substance made it in.
    assert f"One benchmark task ran {len(case.classifications)} times" in prompt
    for trial in case.classifications:
        assert trial.trial_name in prompt
        assert trial.evidence in prompt
    for marker in case.expects_in_prompt:
        assert marker in prompt


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=_CASE_IDS)
def test_synthetic_reply_roundtrips_the_parse_path(case):
    out = build_block(case).to_verdict(json.dumps(case.response()))

    assert out["verdict"] == case.expected_verdict
    assert out["confidence"] == case.expected_confidence
    model = TaskVerdictModel(**out)
    assert model.is_good is (case.expected_verdict == "accept")


def test_golden_set_stays_representative():
    """The set must keep exercising both verdicts and all three confidence
    levels, or the live backtest degrades into a one-sided check."""
    assert {c.expected_verdict for c in GOLDEN_CASES} == {"accept", "reject"}
    assert {c.expected_confidence for c in GOLDEN_CASES} == {"high", "medium", "low"}
    assert len({c.name for c in GOLDEN_CASES}) == len(GOLDEN_CASES)
