"""Pre-trial on the CLAUDE_CLI backend receives claude-code's stream-json
*result envelope*, not the model's bare answer. The unwrap has to happen
before schema validation, or every audit silently returns zero findings
(extra envelope keys are ignored and ``items`` defaults to empty).
"""

import json

from oddish.blocks.analyzer.pre_trial.pre_trial_block import PreTrialBlock

_ITEM = {
    "source": "pre_trial",
    "problem_type": "incompleteness",
    "dimension": "verifier",
    "file": "verifier.py",
    "line_start": 12,
    "line_end": 14,
    "title": "stderr ignored",
    "detail": "The verifier never checks stderr.",
    "recommendation": "Assert stderr is empty.",
    "tier": "should_fix",
}


def _envelope(model_answer: str) -> str:
    """The result event ClaudeCliClient.stream ends with: the model's text
    answer under ``result`` and no schema-structured output."""
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "structured_output": None,
            "result": model_answer,
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
    )


def _block() -> PreTrialBlock:
    return PreTrialBlock(task_id="t1", trial_ids=[], prompt_template="x")


def test_cli_transform_extracts_items_from_the_envelope():
    raw = _envelope(json.dumps({"items": [_ITEM]}))
    out = _block().to_action_items_from_cli(raw)
    assert [i["title"] for i in out["items"]] == ["stderr ignored"]


def test_cli_transform_tolerates_a_bare_array():
    # Models sometimes return a bare JSON array; the CLI transform wraps it just
    # like PreTrialBlock.parse_json does for the string path.
    raw = _envelope(json.dumps([_ITEM]))
    out = _block().to_action_items_from_cli(raw)
    assert len(out["items"]) == 1


def test_cli_transform_handles_code_fenced_result():
    raw = _envelope("```json\n" + json.dumps({"items": [_ITEM]}) + "\n```")
    out = _block().to_action_items_from_cli(raw)
    assert len(out["items"]) == 1


def test_naive_transform_would_silently_drop_findings():
    # The regression the CLI transform exists to prevent: feeding the envelope
    # straight to to_action_items validates (extra keys ignored) and yields zero
    # items rather than raising -- a silently empty audit on every run.
    raw = _envelope(json.dumps({"items": [_ITEM]}))
    assert _block().to_action_items(raw)["items"] == []
