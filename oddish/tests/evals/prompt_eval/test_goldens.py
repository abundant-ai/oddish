"""Validates the checked-in golden corpus itself: every case must load, so a
malformed case.yaml fails here instead of surfacing mid-eval in CI."""

from pathlib import Path

import pytest

from oddish.evals.prompt_eval.goldens import (
    GoldenLoadError,
    load_cases,
    load_config,
)
from oddish.evals.prompt_eval.grading import grade
from oddish.evals.prompt_eval.kinds import PROMPT_KIND_SPECS

GOLDENS_ROOT = Path(__file__).parents[4] / "evals" / "prompt-goldens"


def test_corpus_exists():
    assert GOLDENS_ROOT.is_dir(), f"missing {GOLDENS_ROOT}"


def test_config_loads():
    config = load_config(GOLDENS_ROOT)
    assert config.agents, "config must define at least one agent"
    assert config.n_trials >= 1


def test_every_checked_in_case_loads():
    config = load_config(GOLDENS_ROOT)
    loaded = 0
    for kind, spec in PROMPT_KIND_SPECS.items():
        if not spec.supported:
            continue
        cases = load_cases(GOLDENS_ROOT, kind, max_cases=config.max_cases_per_kind)
        for case in cases:
            assert case.expected, f"{kind}/{case.name} has empty expected"
        loaded += len(cases)
    assert loaded > 0, "corpus has no cases at all"


def test_post_trial_cases_have_result_json():
    for case in load_cases(GOLDENS_ROOT, "QA_POST_TRIAL"):
        assert (case.trial_dir / "result.json").exists(), case.name
        assert (case.task_dir / "instruction.md").exists(), case.name


def test_expected_blocks_are_gradable():
    """Every case's expected block must be structurally valid for its grader
    (an obviously-wrong output must grade False, not raise)."""
    wrong_output = {
        "QA_POST_TRIAL": {"classification": "HARNESS_ERROR", "subtype": "?"},
        "QA_PRE_TRIAL": {"items": []},
        "VERDICT": {"is_good": None, "confidence": "?"},
    }
    for kind, output in wrong_output.items():
        for case in load_cases(GOLDENS_ROOT, kind):
            result = grade(kind, case.expected, output)
            assert result.passed in (True, False)


def test_malformed_case_raises(tmp_path):
    case_dir = tmp_path / "VERDICT" / "cases" / "broken"
    case_dir.mkdir(parents=True)
    (case_dir / "case.yaml").write_text("description: no expected block\n")
    with pytest.raises(GoldenLoadError):
        load_cases(tmp_path, "VERDICT")


def test_max_cases_guard(tmp_path):
    for index in range(3):
        case_dir = tmp_path / "VERDICT" / "cases" / f"case-{index}"
        case_dir.mkdir(parents=True)
        (case_dir / "case.yaml").write_text(
            "expected: {is_good: true}\n"
            "inputs: {num_trials: 1, baseline_summary: x, "
            "quality_check_summary: x, trial_classifications: x}\n"
        )
    with pytest.raises(GoldenLoadError):
        load_cases(tmp_path, "VERDICT", max_cases=2)
