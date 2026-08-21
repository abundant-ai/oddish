# Known Oddish contract traps

Use this reference when prose, UI labels, stored rows, and runtime behavior
appear inconsistent.

| Misleading assumption | Runtime contract |
|---|---|
| The latest numbered task version is active. | `tasks.current_version_id` is the user-selected default and can point to an older version. |
| `current_version_id` on an experiment response identifies the trial rows shown. | `current_version_id` remains the task default; `trial_version_id` is the experiment's trial-selection pivot. |
| Every row in task `trials[]` is an evaluation attempt. | Only `kind == "agent"` is a user evaluation attempt; `qa`, `audit`, and `summarize` are platform analysis trials. |
| Trial `status == "success"` means the verifier passed. | `success` means execution completed; verifier credit is in `reward`. |
| Trial `status == "failed"` means the agent produced a bad solution. | `failed` means an execution/harness error; an ordinary completed zero-reward run can still have `status == "success"`. |
| A gate-skipped model is a model failure. | `skipped` means nop/oracle validation rejected that task version and experiment before the paid trial ran. |
| QA is a separate worker-job type. | QA, audit, and summary refreshes are `TRIAL` jobs whose `trials.kind` identifies the analysis. Legacy worker-job enum values are historical only. |
| A run needs `--run-analysis`. | No such current option exists; QA admission is automatic after current-version agent trials settle and the audit finishes. |
| `backfill-analysis --trial X` analyzes only X. | It clears X's visible stored state, then starts task-wide QA over every eligible trial. |
| Omitting `--force` reuses old classifications. | Every replacement QA pass rereads and reclassifies the eligible set; `--force` controls which stored fields are cleared before it starts. |
| `cancel --qa` affects only the classifier. | The task QA cancellation endpoint cancels live `qa` and `audit` trials. |
| A replacement QA run immediately hides the old verdict. | The last successful verdict can remain published while the replacement is queued or running. |
| Every command supports `--json` except logs. | JSON support is declared per command; link and probe paths also lack it. |
| `run --json` returns a guaranteed `experiment_id`. | It returns the experiment name and URL; retain task IDs and parse the URL only if a caller explicitly accepts that coupling. |
| Publishing always needs a full-scope key. | An admin-created `tasks` key can publish; a member-created `tasks` key cannot. |
| A retry updates the old trial row. | It creates a new immutable trial and links the old row through `superseded_by_trial_id`. |
| Worker job `BLOCKED` is unused. | Baseline gating actively holds paid `TRIAL` jobs in `BLOCKED` until baseline settlement. |

When checking source, use this precedence:

1. Runtime enums and constants for value vocabularies.
2. Shared predicates for membership and eligibility.
3. Response schemas and serializers for fields.
4. Endpoint/service code for lifecycle behavior.
5. Typer command definitions for CLI options and output.
6. `AGENTS.md`, then `DOCS.md` and package README prose.

Do not use implementation plans, handoff notes, or old pull-request text as a
current contract unless the runtime source confirms the same behavior.
