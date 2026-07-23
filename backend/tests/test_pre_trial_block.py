import json

from oddish.blocks.analyzer.pre_trial import pre_trial_prompts
from oddish.blocks.analyzer.pre_trial.pre_trial_block import PreTrialBlock


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


def test_pre_trial_section_joins_trial_ids():
    prompt = pre_trial_prompts.pre_trial_section(
        "task_abc", ["t1", "t2", "t3"], "Trials so far: {trial_ids}"
    )
    assert prompt == "Trials so far: t1, t2, t3"


def test_pre_trial_section_empty_trial_ids_uses_placeholder():
    prompt = pre_trial_prompts.pre_trial_section(
        "task_abc", [], "Trials so far: {trial_ids}"
    )
    assert prompt == "Trials so far: (none yet)"


def test_pre_trial_section_substitutes_task_id_and_trial_ids_together():
    prompt = pre_trial_prompts.pre_trial_section(
        "task_abc", ["t1"], "Task {task_id} has run: {trial_ids}"
    )
    assert prompt == "Task task_abc has run: t1"
