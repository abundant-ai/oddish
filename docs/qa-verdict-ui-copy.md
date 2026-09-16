# QA verdict UI wording changes

This change standardizes task-level review/result wording as QA verdict. It does not change QA execution, verdict rules, eligibility, request targets, costs, stored field names, filtering, or human sign-off. Error explanations and the distinctions between absent, failed, queued, running, stale, and accepted/rejected verdicts remain visible.

QA cost stays QA cost. Pre-trial audit, findings, trajectory summaries, trial classifications, mixed QA history, QA work ownership, and human actions such as “Review evidence” retain their separate meanings. Cancellation stays “Cancel QA” because it can stop audits as well as task QA. The analysis used to produce the verdict is labeled Run QA Verdict; the recorded GOOD/BAD/HARNESS outcomes retain their meanings. The run-analysis legend keeps its separate meaning; its shared error label is made explicit so changing verdict labels cannot call a solver harness error a failed task verdict.

## Complete before-and-after list

All changed user-visible labels, headings, tooltips, accessible action names, notification preference descriptions, and fallback messages are listed below. Repeated replacements are grouped. `[count]`, `[version]`, and `[for vN]` stand for existing interpolated values; their values and selection logic are unchanged. Fixture names and internal identifiers are not UI vocabulary and are not renamed.

| Before | After | Surfaces |
| --- | --- | --- |
| No result for this version | No QA verdict for this version | `lib/review.ts` |
| Review queued | QA verdict queued | `lib/review.ts`, `lib/deliveries.ts` |
| Review running | QA verdict running | `lib/review.ts`, `lib/deliveries.ts` |
| Review couldn’t finish | QA verdict failed | `lib/review.ts` |
| Not reviewed | No QA verdict | `lib/review.ts`, `lib/deliveries.ts` |
| Blocking defects found | Rejected | `lib/deliveries.ts` |
| Review needs refresh | QA verdict needs refresh | `lib/deliveries.ts` |
| Review could not complete | QA verdict failed | `lib/deliveries.ts` |
| Verdict needed | QA verdict needed | `lib/deliveries.ts`, `core/deliveries.py` |
| No overall result | No QA verdict generated | `components/task-verdict-badge.tsx`, `components/experiment-trials-table.tsx` |
| Review runs [for vN] | Generate QA verdict [for vN] | `components/task-verdict-badge.tsx`, `components/task-files-panel.tsx` |
| Queuing review… | Queuing QA verdict… | `components/task-verdict-badge.tsx` |
| Run reviews | QA verdict | `components/task-verdict-badge.tsx` |
| QA result · | QA verdict · | `components/task-overview-panel.tsx` |
| QA results | QA verdicts | `components/experiment-detail-view.tsx` |
| In progress | QA verdict in progress | `components/experiment-detail-view.tsx` |
| Review error | QA verdict failed | `components/experiment-detail-view.tsx` |
| No current result | No current QA verdict | `components/experiment-detail-view.tsx` |
| Review could not complete | QA failed / Harness error | `components/experiment-trials-table.tsx` |
| not included in this result | not included in this QA verdict | `components/experiment-trials-table.tsx` |
| Open QA overview | Open QA verdict | `components/experiment-trials-table.tsx` |
| Review runs for each selected task’s default version. | Generate a QA verdict for each selected task’s default version by reanalyzing its eligible trials. | `components/experiment-trials-table.tsx` |
| Queueing / Review runs | Queuing QA verdicts… / Generate QA verdicts | `components/experiment-trials-table.tsx` |
| Cancel task checks and run reviews for selected tasks. | Cancel pre-trial audits and QA verdict generation for selected tasks. | `components/experiment-trials-table.tsx` |
| Cancelling / Cancel reviews | Cancelling / Cancel QA | `components/experiment-trials-table.tsx` |
| QA pending | QA verdict pending | `app/(app)/dashboard/dashboard-client.tsx` |
| Task-level QA is already running | QA verdict is running | `components/trial-detail-panel.tsx` |
| Re-run analysis | Regenerate QA verdict | `components/trial-detail-panel.tsx` |
| Run analysis | Generate QA verdict | `components/trial-detail-panel.tsx` |
| (no explanation on the enabled button) | Reanalyzes all eligible trials to generate this task’s QA verdict. | `components/trial-detail-panel.tsx` |
| verdict: | QA verdict: | `app/(app)/deliveries/[delivery]/delivery-board-client.tsx` |
| Verdict | QA verdict | `app/(app)/deliveries/[delivery]/delivery-board-client.tsx` |
| Open run review | Open QA verdict | `app/(app)/deliveries/[delivery]/delivery-board-client.tsx` |
| Review status and checks | QA verdict status and delivery checks | `app/(app)/deliveries/[delivery]/delivery-board-client.tsx` |
| Review finished | QA verdict generation finished | `app/(app)/deliveries/[delivery]/delivery-board-client.tsx` |
| Open execution-review run | Open QA verdict run | `app/(app)/deliveries/[delivery]/delivery-board-client.tsx` |
| Rerun QA ([count]) | Regenerate QA verdicts ([count]) | `app/(app)/deliveries/[delivery]/delivery-board-client.tsx` |
| Requested QA for [count] tasks | Requested QA verdict generation for [count] tasks | `app/(app)/deliveries/[delivery]/delivery-board-client.tsx` |
| Last QA | Last QA verdict run | `app/(app)/deliveries/[delivery]/delivery-board-client.tsx` |
| QA failed | QA verdict rejected or unavailable | `app/(app)/settings/page.tsx` |
| A task's QA verdict came back bad. | A task was rejected or its QA verdict could not be generated. | `app/(app)/settings/page.tsx` |
| Task finished | QA verdict accepted | `app/(app)/settings/page.tsx` |
| A task's QA verdict came back good. | A task received an Accepted QA verdict. | `app/(app)/settings/page.tsx` |
| QA covers a different task version | QA verdict covers a different task version | `core/delivery_qa.py` |
| QA is running | QA verdict generation is running | `core/delivery_qa.py` |
| QA is queued | QA verdict generation is queued | `core/delivery_qa.py` |
| QA did not complete | QA verdict generation did not complete | `core/delivery_qa.py` |
| QA completion time was not recorded | QA verdict generation completion time was not recorded | `core/delivery_qa.py` |
| QA evidence coverage was not recorded; rerun QA | QA verdict evidence coverage was not recorded; regenerate the QA verdict | `core/delivery_qa.py` |
| Trials changed since QA; rerun QA | Trials changed since the QA verdict run; regenerate the QA verdict | `core/delivery_qa.py` |
| Source audit changed since QA; rerun QA | Source audit changed since the QA verdict run; regenerate the QA verdict | `core/delivery_qa.py` |
| QA produced no current verdict | No current QA verdict was generated | `core/delivery_qa.py` |
| QA accepts the current version and trials | QA verdict accepts the current version and trials | `core/delivery_qa.py` |
| QA rejects the current version | QA verdict rejects the current version | `core/delivery_qa.py` |
| No blocking defects in verdict | No blocking defects in QA verdict | `core/deliveries.py` |
| Verdict queued | QA verdict queued | `core/deliveries.py` |
| Verdict running | QA verdict running | `core/deliveries.py` |
| Verdict failed | QA verdict failed | `core/deliveries.py` |
| no completed execution-review verdict on [version] | no completed QA verdict on [version] | `core/deliveries.py` |
| verdict does not cover [version]; re-run QA on it | QA verdict does not cover [version]; regenerate the QA verdict for it | `core/deliveries.py` |
| review found no blocking defects; human sign-off is separate | QA verdict found no blocking defects; human sign-off is separate | `core/deliveries.py` |
| No QA result recorded | No QA verdict recorded | `schemas.py` |
| Review costs are listed separately. | QA costs are listed separately. | `components/experiment-detail-view.tsx` |
| Review cost across | QA cost across | `components/experiment-detail-view.tsx` |
| estimated review costs | estimated QA costs | `components/experiment-detail-view.tsx` |
| Review cost on | QA cost on | `components/experiment-detail-view.tsx` |
| No tasks are ready for QA. | No tasks are ready for QA verdict generation. | `components/experiment-trials-table.tsx` |
| Failed to queue task QA | Failed to queue QA verdict generation | `components/experiment-trials-table.tsx`, `components/task-files-panel.tsx` |
| Failed to queue QA for [count] task(s). | Failed to queue QA verdict generation for [count] task(s). | `components/experiment-trials-table.tsx` |
| Failed to queue analysis | Failed to queue QA verdict generation | `components/trial-detail-panel.tsx` |
| Failed to queue QA | Failed to queue QA verdict generation | `app/(app)/tasks/[task_id]/task-detail-client.tsx` |
| QA request failed | QA verdict request failed | `app/(app)/deliveries/[delivery]/delivery-board-client.tsx` |
| Review running | QA verdict in progress | `components/experiment-trials-table.tsx` |
| Review error | QA failed | `components/experiment-trials-table.tsx` |
| Analysis is already running for this trial | QA is running for this trial | `components/trial-detail-panel.tsx` |
| Analysis is already queued for this trial | QA is queued for this trial | `components/trial-detail-panel.tsx` |
| The trial must finish before analysis can run | QA requires a finished trial | `components/trial-detail-panel.tsx` |
| Analysis failed: [error] | QA failed: [error] | `components/trial-detail-panel.tsx` |
| Analyzing | QA running | `components/trial-detail-panel.tsx` |
| Analysis queued | QA queued | `components/trial-detail-panel.tsx` |
| QA is running | QA verdict running | `components/trial-detail-panel.tsx` |
| Analysis | Run QA Verdict | `components/trial-detail-panel.tsx` |
| No report was produced. | No QA report produced. | `components/trial-detail-panel.tsx` |
| No analysis yet | No QA verdict yet | `components/trial-detail-panel.tsx` |
| view the QA run | Open QA run | `components/trial-detail-panel.tsx` |
| [count] awaiting review | [count] awaiting QA | `components/task-overview-panel.tsx` |
| Trajectory analysis | Run QA Verdict | `components/task-overview-panel.tsx` |
| ANALYSIS RUNNING | QA RUNNING | `components/task-overview-panel.tsx` |
| ANALYSIS FAILED | QA FAILED | `components/task-overview-panel.tsx` |
| NOT ANALYZED | NO QA VERDICT YET | `components/task-overview-panel.tsx` |
| The analysis produced no root cause. | QA produced no root cause. | `components/qa-report/qa-assessment-report.tsx` |
| Run review | Run QA Verdict | `components/task-verdict-badge.tsx`, `app/(app)/tasks/[task_id]/task-detail-client.tsx` |
| Execution review | QA verdict | `app/(app)/deliveries/[delivery]/delivery-board-client.tsx` |
| Analysis result | QA Verdict Results | `lib/tasks-filters.ts` |
| source review: | Pre-trial audit: | `app/(app)/deliveries/[delivery]/delivery-board-client.tsx` |
| source review could not complete: | Pre-trial audit could not complete: | `app/(app)/deliveries/[delivery]/delivery-board-client.tsx` |
| Verdict pending | QA verdict pending | `lib/tasks-filters.ts` |

## Validation

- Existing verdict, delivery, and task-action tests exercise state mapping, accepted/rejected results, missing/outdated states, and unchanged action guards. The failure presentation check also confirms its original diagnostic text is retained.
- Frontend TypeScript checking and ESLint on changed TypeScript files pass.
- The three Python files have identical syntax trees after replacing string contents with a placeholder. Their calculations, conditions, state writes, and calls are unchanged.
- Direct calls to delivery QA presentation cover version mismatch, queued, running, failed with retained evidence error, and missing completion time.
- The local fixture UI shows the new task-verdict labels and retains the original failure explanation. It uses fixture data, not live QA executions.
- Twelve targeted browser tests passed: six delivery scenarios and six QA scenarios. They retain checks for request counts, selected versions, history refresh, requirement links, failure explanations, finding links, filtered rows, and Back/reload pressed-state persistence.
