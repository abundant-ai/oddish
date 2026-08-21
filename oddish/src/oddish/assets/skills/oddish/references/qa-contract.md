# Oddish QA contract

Use this reference when reading or rerunning pre-trial findings, per-trial
classifications, task verdicts, action items, or trajectory summaries.

## Analysis runs are trials

Oddish stores platform analysis in the `trials` table and executes it through
the normal `TRIAL` worker-job kind:

- `kind = "audit"` reviews task source once per task version and writes
  `audit_result.json`.
- `kind = "qa"` reviews the eligible agent-trial set for one task version and
  writes `qa_result.json`.
- `kind = "summarize"` refreshes one agent trial's trajectory summary on demand
  and writes `summary_result.json`.

The retired `QA`, `VERDICT`, `ANALYSIS`, `QA_REVIEW`, `ANALYZER`, and
`ANALYZER_BLOCK` worker-job enum values exist only so historical database rows
remain readable. Do not wait for or enqueue those job kinds.

Every analysis trial has at most 3 attempts and a 60-minute timeout. Its own
cost is analysis spend, not an additional evaluation attempt.

## Automatic task QA

For a new run or append, no `--run-analysis` flag exists. Oddish automatically
admits task QA after every nonsuperseded current-version agent trial is
terminal. Admission waits for the live pre-trial audit because the QA brief
embeds that audit's findings.

The QA-eligible set is current-version, `kind = "agent"`, nonsuperseded,
not cancelled, not skipped, not gate-skipped, and not a nop/oracle baseline.
Rows with `imported_at` set are excluded. A `failed` agent trial can still be
eligible because QA can classify a harness or task failure from its evidence.

QA always attempts classifications and trajectory summaries for its eligible
set. It requests a task verdict only when the set has at least 5 trials from at
least 3 distinct agent names. Below that evidence bar, the task can complete
with classifications and no verdict.

## Classification and verdict fields

Per-trial classifications are:

- `GOOD_SUCCESS`: the verifier passed and QA accepts the task/run behavior.
- `BAD_SUCCESS`: the verifier passed but QA found a task defect or invalid pass.
- `GOOD_FAILURE`: the verifier failed without showing a task defect.
- `BAD_FAILURE`: the verifier failed because QA found a task defect.
- `HARNESS_ERROR`: execution evidence indicates a harness or infrastructure
  failure rather than an ordinary verifier outcome.

Keep `status`, `reward`, and `analysis.classification` separate. `success`
means execution completed, `reward` is verifier credit, and the classification
is QA's judgment of why that result occurred.

The task verdict contains `verdict` (`accept` or `reject`), `is_good`,
`confidence`, `primary_issue`, `reasoning`, and task-level `recommendations`.
The full individual trial record adds `analysis.root_cause`,
`analysis.recommendation`, and `analysis.action_items[]`.

Each action item can carry `source` (`pre_trial` or `post_trial`),
`problem_type`, `dimension`, `file`, one-based `line_start` and `line_end`,
`title`, `detail`, `recommendation`, `tier` (`must_fix`, `should_fix`, or
`optional`), and exploitation linkage fields. Task status can trim embedded
trial analysis; use `oddish status <trial_id> --json` for the full record.

## Replacement and backfill semantics

`oddish backfill-analysis --task <task_id>` and
`oddish run <task_id> --retry --qa` create a replacement task-wide QA pass.
The pass rereads and reclassifies every eligible trial even without `--force`.

`--force` and `--trial <trial_id>` control which stored analysis fields are
cleared before the replacement starts; they do not narrow the QA trial's input
set. Therefore `backfill-analysis --trial X` is still a task-wide QA run.

While a replacement is queued or running, the last successful verdict may
remain visible. A successful replacement publishes the new result; cancelling
the replacement restores the prior successful verdict state; a terminal
replacement failure clears the preserved verdict according to the verdict
state machine.

`oddish cancel <task_id> --qa` cancels live `qa` and `audit` trials for that
task. The CLI label says QA, but the endpoint also stops the pre-trial audit.
It does not target an independent legacy QA worker job.
