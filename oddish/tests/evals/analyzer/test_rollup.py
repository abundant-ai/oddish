"""build_rollup is pure: dicts in, dicts out. No DB, no LLM, no fixtures."""

from oddish.evals.analyzer.rollup import build_rollup


def _f(**over) -> dict:
    """A persisted finding dict (asdict(Finding)) with sane defaults."""
    base = {
        "trial_id": "t1", "bucket": "bad", "subcategory": "1b",
        "evidence_quote": "q", "step_ids": [3], "root_cause": "rc",
        "headroom_signal": "", "trajectory_link": "/tasks/task/probe/t1",
        "model": "claude-opus-4-8", "classification": "BAD_FAILURE",
        "subtype": "Hardcoding", "task_id": "task", "task_path": "tasks/task",
    }
    base.update(over)
    return base


def test_lane_comes_from_the_classifier_not_the_llm():
    """The LLM says good/3b; the classifier says BAD_FAILURE/Hardcoding. The
    classifier wins, or a hackable task gets laundered into capability headroom."""
    out = build_rollup([_f(bucket="good", subcategory="3b")], {"task": ["claude-opus-4-8"]})
    assert out["reward_hacking"]["groups"], "should be in the 1b lane"
    assert not out["good_failures"]["groups"]


def test_good_failure_lands_in_the_good_lane():
    out = build_rollup(
        [_f(classification="GOOD_FAILURE", subtype="Logic Error", bucket="bad",
            subcategory="1a")],
        {"task": ["claude-opus-4-8"]},
    )
    assert out["good_failures"]["groups"][0]["key"] == "claude-opus-4-8"
    assert out["good_failures"]["groups"][0]["gaps"][0]["gap"] == "Logic Error"
    assert out["good_failures"]["groups"][0]["gaps"][0]["subcategory"] == "3b"


def test_1a_lands_in_task_construction_and_groups_by_task():
    out = build_rollup(
        [_f(subtype="Ambiguous Requirements", task_path="tasks/alpha", task_id="alpha")],
        {"alpha": ["claude-opus-4-8", "claude-haiku-4-5"]},
    )
    g = out["task_construction"]["groups"][0]
    assert out["task_construction"]["axis"] == "task"
    assert g["key"] == "tasks/alpha"
    assert g["task_id"] == "alpha"


def test_models_hit_ratio_uses_the_roster_not_the_findings():
    """A task run by 4 models but failed by 1 must read 1/4. Findings only record
    failures, so a denominator derived from them would say 1/1."""
    out = build_rollup(
        [_f(subtype="Underspecified Instruction", task_id="alpha", task_path="tasks/alpha")],
        {"alpha": ["claude-opus-4-8", "claude-haiku-4-5", "gpt-5", "gemini-3"]},
    )
    g = out["task_construction"]["groups"][0]
    assert g["models_hit"] == ["claude-opus-4-8"]
    assert g["models_total"] == 4


def test_ratio_normalizes_both_sides():
    """Roster holds the same model under two spellings (plain + Bedrock). If the
    roster side skipped normalization, these would count as 2 distinct models
    instead of collapsing to 1 — inflating the denominator and letting
    models_hit exceed the roster."""
    out = build_rollup(
        [_f(subtype="Missing File Reference", model="claude-opus-4-8",
            task_id="alpha", task_path="tasks/alpha")],
        {"alpha": ["claude-opus-4-8", "global.anthropic.claude-opus-4-8"]},
    )
    g = out["task_construction"]["groups"][0]
    assert g["models_total"] == 1
    assert set(g["models_hit"]) <= {"claude-opus-4-8"}


def test_model_spellings_collapse_to_one_group():
    out = build_rollup(
        [
            _f(trial_id="t1", classification="GOOD_FAILURE", subtype="Logic Error",
               model="claude-opus-4-8"),
            _f(trial_id="t2", classification="GOOD_FAILURE", subtype="Logic Error",
               model="anthropic/claude-opus-4-8"),
            _f(trial_id="t3", classification="GOOD_FAILURE", subtype="Logic Error",
               model="global.anthropic.claude-opus-4-8"),
        ],
        {"task": ["claude-opus-4-8"]},
    )
    groups = out["good_failures"]["groups"]
    assert len(groups) == 1
    assert groups[0]["gaps"][0]["count"] == 3


def test_none_model_groups_as_unknown():
    out = build_rollup(
        [_f(classification="GOOD_FAILURE", subtype="Logic Error", model=None)],
        {"task": []},
    )
    assert out["good_failures"]["groups"][0]["key"] == "unknown"


def test_gaps_sorted_by_count_desc():
    """Insertion order is the 1-count gap first, then the 3-count gap — the
    opposite of count-desc — so a missing sort would return them unsorted."""
    findings = (
        [_f(trial_id="b1", classification="GOOD_FAILURE", subtype="Wrong Approach")]
        + [_f(trial_id=f"a{i}", classification="GOOD_FAILURE", subtype="Logic Error")
           for i in range(3)]
    )
    out = build_rollup(findings, {"task": ["claude-opus-4-8"]})
    gaps = out["good_failures"]["groups"][0]["gaps"]
    assert [g["gap"] for g in gaps] == ["Logic Error", "Wrong Approach"]
    assert [g["count"] for g in gaps] == [3, 1]


def test_gaps_tied_by_count_break_ties_by_key():
    """Two gaps with equal counts, inserted in reverse-alphabetical order — a
    stable sort on count alone would leave them in insertion order."""
    findings = [
        _f(trial_id="w1", classification="GOOD_FAILURE", subtype="Wrong Approach"),
        _f(trial_id="w2", classification="GOOD_FAILURE", subtype="Wrong Approach"),
        _f(trial_id="c1", classification="GOOD_FAILURE", subtype="Context Loss"),
        _f(trial_id="c2", classification="GOOD_FAILURE", subtype="Context Loss"),
    ]
    out = build_rollup(findings, {"task": ["claude-opus-4-8"]})
    gaps = out["good_failures"]["groups"][0]["gaps"]
    assert [g["gap"] for g in gaps] == ["Context Loss", "Wrong Approach"]
    assert [g["count"] for g in gaps] == [2, 2]


def test_examples_carry_what_the_ui_needs():
    out = build_rollup(
        [_f(classification="GOOD_FAILURE", subtype="Logic Error", step_ids=[3, 7])],
        {"task": ["claude-opus-4-8"]},
    )
    ex = out["good_failures"]["groups"][0]["gaps"][0]["examples"][0]
    assert ex["trial_id"] == "t1"
    assert ex["trajectory_link"] == "/tasks/task/probe/t1"
    assert ex["step_ids"] == [3, 7]
    assert ex["root_cause"] == "rc"
    assert ex["evidence_quote"] == "q"


def test_unmapped_subtype_is_emergent_not_a_crash():
    out = build_rollup(
        [_f(classification="GOOD_FAILURE", subtype="Something Novel")],
        {"task": ["claude-opus-4-8"]},
    )
    assert out["good_failures"]["groups"][0]["gaps"][0]["subcategory"] == "emergent"


def test_other_bucket_is_dropped():
    """GOOD_SUCCESS/HARNESS_ERROR map to 'other' — not a failure, no lane."""
    out = build_rollup(
        [_f(classification="HARNESS_ERROR", subtype="Timeout")], {"task": []}
    )
    assert not any(out[k]["groups"] for k in
                   ("good_failures", "task_construction", "reward_hacking"))


def test_empty_findings_yields_three_empty_lanes():
    out = build_rollup([], {})
    for lane in ("good_failures", "task_construction", "reward_hacking"):
        assert out[lane]["groups"] == []


def test_task_missing_from_roster_reports_zero_total():
    """A finding whose task isn't in the roster must not KeyError."""
    out = build_rollup(
        [_f(subtype="Ambiguous Requirements", task_id="ghost", task_path="tasks/ghost")],
        {},
    )
    assert out["task_construction"]["groups"][0]["models_total"] == 0
