from __future__ import annotations


_TEMPLATE = """\
# Experiment {experiment_id}

You are a Claude Code agent helping a user reason about the artifacts of
an Oddish experiment. The experiment's full artifact tree is mounted at
the current working directory under `jobs/{experiment_id}/`.

## Layout

```
jobs/{experiment_id}/
  <trial_id>/
    config.json          # trial config (task, agent, model, seed)
    result.json          # final reward, status, durations
    trial.log            # high-level trial events
    exception.txt        # present if the trial errored
    agent/
      claude-code.txt    # raw stdout/stderr from the agent
      trajectory.json    # parsed message+action timeline
      sessions/          # raw Claude Code session state
    verifier/
      ctrf.json          # structured test results (CTRF format)
      reward.txt         # numeric reward as a string
      test-stdout.txt    # raw verifier stdout
```

## How to navigate

- Start with `jobs/{experiment_id}/<trial_id>/result.json` and
  `jobs/{experiment_id}/<trial_id>/config.json` to get a trial's verdict
  and inputs without reading anything heavy.
- Use `Glob` and `Grep` to find things across trials. For example,
  `Grep --files-with-matches "FAIL" jobs/{experiment_id}/`.
- Only read full `trial.log` / `agent/claude-code.txt` / `agent/trajectory.json`
  when the user has zoomed into a specific trial. These can be large.
- `verifier/ctrf.json` is the canonical "did the test pass?" file.

## Trials in this experiment
{trial_list}
"""

_EMPTY_TRIAL_BLOCK = "_(no trial data available yet)_"


def render_claude_md(*, experiment_id: str, trial_ids: list[str]) -> str:
    if trial_ids:
        trial_list = "\n".join(f"- `{tid}`" for tid in sorted(trial_ids))
    else:
        trial_list = _EMPTY_TRIAL_BLOCK
    return _TEMPLATE.format(
        experiment_id=experiment_id, trial_list=trial_list
    )
