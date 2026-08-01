from oddish.evals.prompt_eval.report import COMMENT_MARKER, render_comment


def _record(case, agent, passed, *, trial=1, detail="", error=False):
    return {
        "case": case,
        "agent": agent,
        "model": f"model-{agent}",
        "trial": trial,
        "passed": passed,
        "detail": detail,
        "error": error,
    }


def _report(**overrides):
    report = {
        "kind": "QA_POST_TRIAL",
        "prompt_path": "oddish/src/oddish/analyze/classify_prompt.txt",
        "base_sha": "abc1234",
        "head_sha": "def5678",
        "agents": [{"name": "sonnet", "model": "model-sonnet"}],
        "n_trials": 1,
        "baseline": None,
        "candidate": None,
        "note": None,
    }
    report.update(overrides)
    return report


def test_comparison_table_shows_rates_and_delta():
    comment = render_comment(
        [
            _report(
                baseline=[
                    _record("case-a", "sonnet", False, detail="classification BAD"),
                    _record("case-b", "sonnet", True),
                ],
                candidate=[
                    _record("case-a", "sonnet", True),
                    _record("case-b", "sonnet", True),
                ],
            )
        ]
    )
    assert COMMENT_MARKER in comment
    assert "1/2 (50%)" in comment
    assert "2/2 (100%)" in comment
    assert "+50pp" in comment
    assert "`case-a`" in comment


def test_candidate_only_has_no_base_column():
    comment = render_comment([_report(candidate=[_record("case-a", "sonnet", True)])])
    assert "Δ" not in comment
    assert "1/1 (100%)" in comment


def test_note_only_report_renders_note():
    comment = render_comment(
        [_report(note="⚠ This prompt changed but has no goldens.")]
    )
    assert "no goldens" in comment


def test_error_runs_are_flagged():
    comment = render_comment(
        [
            _report(
                candidate=[
                    _record(
                        "case-a", "sonnet", False, detail="run error: boom", error=True
                    )
                ]
            )
        ]
    )
    assert "⚠1 err" in comment
    assert "run error: boom" in comment


def test_detail_pipes_are_escaped():
    comment = render_comment(
        [_report(candidate=[_record("case-a", "sonnet", False, detail="a|b")])]
    )
    assert "a\\|b" in comment
