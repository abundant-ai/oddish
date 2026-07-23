"""Adapt the DB-registry pre-trial prompt into the Block section contract."""

from __future__ import annotations


def pre_trial_section(task_id: str, trial_ids: list[str], prompt_template: str) -> str:
    trials = ", ".join(trial_ids) if trial_ids else "(none yet)"
    # Targeted substitution rather than str.format: the DB-registry template
    # is free text an operator can edit, and any literal `{...}` in it (JSON
    # example, curly quote, etc.) would make a bare .format raise KeyError.
    return prompt_template.replace("{task_id}", task_id).replace("{trial_ids}", trials)
