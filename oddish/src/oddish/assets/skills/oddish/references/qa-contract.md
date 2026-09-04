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
set. It requests a task verdict only when the set has at least 3 trials from at
least 3 distinct agent names. Below that evidence bar, the task can complete
with classifications and no verdict.

## Classification and verdict fields

Per-trial classifications are:

- `GOOD_SUCCESS`: the verifier passed and QA accepts the task/run behavior.
- `BAD_SUCCESS`: the verifier passed but QA found a task defect or invalid pass.
- `GOOD_FAILURE`: the verifier failed without showing a task defect.
- `BAD_FAILURE`: the verifier failed because QA found a task defect.
- `HARNESS_ERROR`: execution evidence indicates a harness or infrastructure
  failure rather than an ordinary verifier outcome. Exception: a
  `HARNESS_ERROR` with `subtype = "hidden_file_leak"` counts as a task
  defect, not an infra failure.

Keep `status`, `reward`, and `analysis.classification` separate. `success`
means execution completed, `reward` is verifier credit, and the classification
is QA's judgment of why that result occurred.

The task verdict contains `verdict` (`accept` or `reject`), `is_good`,
`confidence`, `primary_issue`, `reasoning`, task-level `recommendations`, and
four server-recomputed counts that model output cannot inflate:
`task_problem_count`, `agent_problem_count`, `success_count`, and
`harness_error_count`.
The full individual trial record's `analysis` blob carries the whole
classification entry: `classification`, `subtype`, `evidence`, `root_cause`,
`recommendation`, `action_items[]`, `exploitation[]`, plus internal
`_graded_by` / `_graded_at_steps` keys.

Each action item carries a server-computed `id` (the target of other items'
`links_to`) and can carry `source` (`pre_trial` or `post_trial`),
`problem_type`, `dimension`, `file`, one-based `line_start` and `line_end`,
`title`, `detail`, `recommendation`, `tier` (`must_fix`, `should_fix`, or
`optional`), and exploitation linkage fields (`links_to`, `exploited`,
`exploit_evidence`, `causal`). Task status can trim embedded trial analysis;
use `oddish status <trial_id> --json` for the full record.

## Replacement and backfill semantics

`oddish backfill-analysis --task <task_id>` and
`oddish run <task_id> --retry --qa` create a replacement task-wide QA pass.
The pass rereads and reclassifies every eligible trial even without `--force`.

`--force` and `--trial <trial_id>` control which stored analysis fields are
cleared before the replacement starts; they do not narrow the QA trial's input
set. Therefore `backfill-analysis --trial X` is still a task-wide QA run.

Before a replacement is queued, Oddish uses the same artifact readers available
to the QA sandbox to check every eligible source trial. A started trial must
have a readable Harbor result and verifier stdout, stderr, or exception. A row
with `has_trajectory = true` must have a readable trajectory JSON object. A
present but empty verifier stream satisfies the verifier requirement. For
historical rows, a malformed Claude trajectory can be rebuilt from the captured
`claude-code.txt` in the same manifest-selected attempt. A missing attempt
pointer can be inferred only when the row is finished, its attempt count matches,
and storage contains exactly one immutable attempt prefix. A failed preflight
returns the blocking trial IDs without clearing analysis, withdrawing the
current verdict, or spending QA attempts.

Queuing a replacement clears the current verdict. While it is queued or
running, the page shows QA in progress. Completion publishes only the new
verdict; if QA produced classifications without an overall verdict, the page
shows "No current verdict". Cancelling, failing, or abandoning the active
replacement does not restore the previous verdict. Older QA artifacts remain
in trial storage.

`oddish cancel <task_id> --qa` cancels live `qa` and `audit` trials for that
task. The CLI label says QA, but the endpoint also stops the pre-trial audit.
It does not target an independent legacy QA worker job.

## QA review votes

Dashboard users can vote on QA output. Votes persist in the append-only core
`feedback` table: `target` is `qa_verdict` or `qa_action_item`, `vote` is
`agree` or `disagree`, with optional free-text `body`, scoped by org and
experiment. The hosted route is `POST /experiments/{experiment_id}/feedback`
(a `tasks`-scope key suffices; 404 when the named trial is not in that
experiment). Votes never alter the stored verdict or classifications — they
are review signal only, and there is no CLI command for them.
