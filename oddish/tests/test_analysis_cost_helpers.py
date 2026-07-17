from oddish.analyze.analysis_cost import (
    AnalysisUsage,
    build_analysis_cost_row,
    parse_cli_usage,
    should_record_cost,
)

_CLI_ENVELOPE = {
    "type": "result",
    "total_cost_usd": 0.0123,
    "result": "{...}",
    "usage": {
        "input_tokens": 1500,
        "output_tokens": 200,
        "cache_read_input_tokens": 800,
        "cache_creation_input_tokens": 64,
    },
}


def test_parse_cli_usage_reads_native_cost_and_tokens() -> None:
    u = parse_cli_usage(_CLI_ENVELOPE, "anthropic/claude-sonnet-4")
    assert u is not None
    assert u.cost_usd == 0.0123
    assert u.input_tokens == 1500
    assert u.output_tokens == 200
    assert u.cache_read_tokens == 800
    assert u.cache_write_tokens == 64
    assert u.model == "anthropic/claude-sonnet-4"
    assert u.source == "native"


def test_parse_cli_usage_none_when_no_cost() -> None:
    assert parse_cli_usage({"usage": {"input_tokens": 5}}, "m") is None


def test_parse_cli_usage_tolerates_missing_usage_block() -> None:
    u = parse_cli_usage({"total_cost_usd": 0.5}, "m")
    assert u is not None
    assert u.cost_usd == 0.5
    assert u.input_tokens is None
    assert u.cache_write_tokens is None


def test_build_row_copies_attribution() -> None:
    u = AnalysisUsage(
        cost_usd=0.02,
        input_tokens=10,
        output_tokens=20,
        cache_read_tokens=30,
        cache_write_tokens=40,
        model="anthropic/claude-sonnet-4",
        source="native",
    )
    row = build_analysis_cost_row(
        job_kind="trial_classifier",
        trial_id="trial-abc",
        org_id="org-1",
        experiment_id="exp-1",
        billed_user_id="user-1",
        usage=u,
    )
    assert row.job_kind == "trial_classifier"
    assert row.trial_id == "trial-abc"
    assert row.org_id == "org-1"
    assert row.experiment_id == "exp-1"
    assert row.billed_user_id == "user-1"
    assert row.cost_usd == 0.02
    assert row.input_tokens == 10
    assert row.cache_write_tokens == 40
    assert row.model == "anthropic/claude-sonnet-4"
    assert row.cost_source == "native"


def test_should_record_requires_both_result_and_usage() -> None:
    u = AnalysisUsage(0.02, 1, 1, 0, 0, "m", "native")
    assert should_record_cost({"classification": "GOOD"}, u) is True
    assert should_record_cost(None, u) is False          # analysis failed
    assert should_record_cost({"classification": "GOOD"}, None) is False  # no cost
    assert should_record_cost(None, None) is False
