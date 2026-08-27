# Oddish domain contract

Use this reference when comparing task versions, counting trials,
interpreting state, applying baselines, or retrying an attempt.

## Identities and ownership

- A task is the versioned evaluation definition. Its ID is stable across
  uploads of new versions.
- A task version is one stored task bundle. Its ID is `<task_id>-v<N>`.
- `tasks.current_version_id` is the user-selected default version. It is not
  necessarily the highest version number.
- A trial is one execution or platform analysis run. Its ID is
  `<task_id>-<index>`.
- An experiment groups comparison runs. A task can belong to many experiments,
  while a normal trial has one canonical `experiment_id`. A collection adds
  existing trials through `experiment_trials` without moving their canonical
  experiment.
- In an experiment view, `current_version_id` still reports the task's selected
  default. `trial_version_id` reports the version used to select visible trial
  rows: the selected default when that experiment represents it, otherwise the
  highest represented version.

New trials are pinned to `current_version_id`. Put nop, oracle, and every model
being compared in the same experiment against the same version. A later
model-only append against another version does not share the earlier baseline
evidence.

## Trial kinds and status

`trials.kind` separates user evaluation attempts from platform analysis:

- `agent`: the evaluation attempt to count for rewards, pass rates, quotas,
  comparisons, and normal retry decisions.
- `audit`: the pre-trial source review for one task version.
- `qa`: the task-wide classifier and optional verdict synthesizer.
- `qa_eval`: one isolated candidate-prompt replay over a historical solver trial.
- `summarize`: an on-demand trajectory-summary refresh for one agent trial.

Normal user-facing eligibility means `kind == "agent"`, not a probe,
not superseded, and not soft-deleted. Some task status responses embed analysis
trials, so do not use raw `len(trials)` as the evaluation-attempt count.

Trial statuses are:

- `pending`, `queued`, `running`, `paused`, `retrying`: nonterminal execution
  states. A paused trial still owns its running worker job while Oddish saves or
  restores its Harbor environment.
- `success`: execution completed and produced a result. It does not mean the
  verifier passed; inspect `reward` separately.
- `failed`: the harness, infrastructure, API, timeout, or execution path failed.
- `skipped`: the trial never ran because the nop/oracle baseline gate rejected
  the task version and experiment.

Task pipeline statuses are `pending`, `running`, legacy `analyzing`,
`verdict_pending`, `completed`, and `failed`. Current QA normally moves a task
from `running` to `verdict_pending`; `analyzing` remains for old rows.

## Nop/oracle baselines

Oddish treats names case-insensitively as baselines when they are exactly
`nop` or `oracle`, start with `nop-` or `oracle-`, or start with `agent-nop` or
`agent-oracle`. Their canonical queue key is `nop_oracle`.

Gating requires two switches: the deployment-level `gate_llm_on_baselines`
setting (default off; hosted Oddish deploys enable it via
`ODDISH_GATE_LLM_ON_BASELINES=1`) and the per-submission
`--baseline-gate/--no-baseline-gate` option (default on). On a deployment with
the global flag off, trials are never held regardless of the CLI flag. When
both are on and a sweep contains a baseline and a non-baseline agent, the gate
decision is scoped to one task version and one experiment:

- every present oracle run must have reward exactly `1`;
- every present nop run must have reward exactly `0`;
- `None`, a partial reward, or a wrong extreme makes the task faulty;
- a faulty gate leaves blocked paid trials terminal as `skipped`.

`nop` reward `0` is the expected calibration outcome. Do not average nop into
model quality or treat the zero as a failed execution.

## Retries and reconciliation

Retry history is immutable. A retry creates a new trial ID and sets the old
row's `superseded_by_trial_id` to that replacement. Normal task and experiment
views exclude the superseded row; direct lookup can still retrieve it.

- Explicit single-trial retry is broader than bulk retry and is the right path
  when the user names one attempt.
- Task/experiment bulk retry selects live, nonsuperseded `failed` trials.
- `--no-baseline-gate` additionally includes `skipped` trials and reruns them
  without waiting on baselines.
- Re-submitting the same requested sweep reconciles counts: live and successful
  trials satisfy requested slots, while failed slots receive replacement rows.

Do not infer that a missing row was deleted until checking whether it was
superseded or belongs to a different `trial_version_id`.
