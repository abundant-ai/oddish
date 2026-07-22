import json

from api.services.blocks.analyzer.pre_trial.pre_trial_block import PreTrialBlock


def _block():
    return PreTrialBlock(
        task_id="task_abc",
        trial_ids=["t1", "t2"],
        prompt_template="Audit the task. Run: oddish pull {task_id} --type task --include-task-files -o ./task_src",
    )


def test_prompt_interpolates_task_id():
    prompt = _block().build_prompt()
    assert "task_abc" in prompt
    assert "oddish pull task_abc" in prompt


def test_to_action_items_parses_list_wrapper():
    raw = json.dumps({"items": [{
        "source": "pre_trial", "problem_type": "incompleteness", "dimension": "verifier",
        "file": "verifier.py", "line_start": 3, "line_end": 5,
        "title": "t", "detail": "d", "recommendation": "r", "tier": "must_fix",
    }]})
    out = _block().to_action_items(raw)
    assert out["items"][0]["file"] == "verifier.py"


def test_to_action_items_tolerates_code_fences():
    raw = "```json\n{\"items\": []}\n```"
    assert _block().to_action_items(raw) == {"items": []}
