# Delivery QA meaning and acceptance demonstration

This change presents the existing delivery policy. It does not change severity
thresholds, minimum rollout requirements, waiver rules, or who may sign off.

## Meaning and evidence

| Fact | Stored evidence | Presentation |
| --- | --- | --- |
| Task quality | The published task verdict and source/execution findings | “Blocking defects found” or “No blocking defects found”; findings remain inspectable. |
| Execution outcome | Each recorded trial's analysis classification | Agent succeeded, fair agent failure, invalid success, task-caused failure, or execution not evaluated. |
| Review progress | Verdict/source status, active review jobs, and verdict version provenance | Not reviewed, queued, running, outdated, or review could not complete. An error does not establish task quality. |
| Human commitment | Delivery check and defect acknowledgement records for the selected version | Awaiting sign-off, signed off on a version, or exception acknowledged on a version. |

`frontend/e2e/fixtures/review-records.json` contains ten representative records,
including a completed source review with a blocking verifier defect and an
unrelated fair agent failure, failed reviews, unreviewed/queued/running reviews,
v8 with a favorable v7 verdict, a favorable unsigned version, and a signed version.
The isolated app in `frontend/e2e/review-app` renders the real task, experiment,
and delivery components with these records. Its authentication and API are
fixtures; it has no backend queue or provider credentials. Production routes do
not acquire a fixture or authentication bypass.

## Acceptance sequence

Open `/deliveries/review-demo` in the fixture app. The initial view excludes the
signed version and shows “Task A · v7 · The verifier accepts an empty answer.”
Opening that link selects finding `empty-answer` on v7. “Open tests/test.sh:7”
opens the verifier with line 7 selected, displaying `exit 0`. The overview also
shows “Fair agent failure,” independently of the blocking source defect.

The shared evidence address is:

```text
/tasks/task-a?version=7&drawer=task&finding=empty-answer&taskPane=file&taskFile=tests%2Ftest.sh&taskLines=L7
```

A second browser tab, Back, Forward, and reload restore the same evidence.
An unavailable version keeps its URL and says “Historical version unavailable.”
An unavailable finding or file names the missing evidence; the reader must
explicitly choose “Open the current version” to leave a missing version.
Versioned file reads with missing source metadata stay at the canonical version
directory instead of trying an unversioned task archive. Legacy records whose
only surviving archive cannot be attributed to that version may therefore show
unavailable evidence until the version's source metadata is restored.

The favorable unsigned record stays “Awaiting sign-off on v1.” The browser test
clicks the existing sign-off checkbox, verifies the explicit delivery-check
request, and then shows “Signed off on v1” in the complete inventory. Backend
tests separately verify that blocking findings still require acknowledgement
and that a version change invalidates an earlier sign-off.

## URL and operation boundaries

Experiment review summary selections write `verdict=accepted`, `rejected`,
`running`, `failed`, or `unreviewed` and filter the displayed results. Missing
and outdated reviews are included in “No current review”; individual rows retain
their distinct labels. Delivery views retain the existing URL parameters and
default to blockers plus outstanding sign-offs; `filter=all` restores inventory.
Native history updates let Next populate its own history state, so Back/Forward
also update the query hooks and visible results.

Page reads, filter selections, and finding inspection issue no API mutations in
the browser tests. The server file-read path resolves the selected version in a
read session and reads storage; experiment result streaming uses a read-only
transaction. No new enqueue operation or asynchronous action handler was added.

Existing explicit operations remain:

| Control | Request and actual scope |
| --- | --- |
| Source review rerun | `POST /tasks/{id}/qa/pre-trial`: reviews the default version's instructions, environment, and verifier. It withdraws the published verdict. After import, the existing `maybe_start_task_qa_stage` admission path can automatically queue a replacement execution review. |
| Execution review rerun | `POST /tasks/{id}/qa/retry`: re-reads eligible recorded runs and synthesizes the default version's verdict. It does not launch solver trials. Existing `force` behavior controls which stored analyses are cleared before that review. |
| Sign-off | `PUT /deliveries/{id}/checks` with `check_key=signoff`, the delivery membership ID, and `checked=true`. The existing server requires blockers to be resolved or explicitly acknowledged. |

Missing-access guidance distinguishes omitted task access, broken execution
infrastructure, and unavailable reviewer access. It tells the reader to choose
the remedy from evidence; it does not classify a generic error string.

## Validation

Backend tests use disposable local PostgreSQL. The combined delivery, review,
task-open, task-panel, experiment-open, statement-budget, storage/source-selection,
and missing-object suites reported `165 passed in 5.20s`. The frontend Node
suite reported `98` passed and `0` failed, including action availability while
review data loads. The representative-record/browser suite reported
`18 passed (11.1s)`; six existing rejection-state checks also passed. Both main
and E2E TypeScript checks exited 0, changed-file ESLint and Ruff checks passed,
and the production fixture build completed successfully.

To run the isolated browser suite after installing frontend dependencies:

```sh
cd frontend
E2E_REVIEW_FIXTURES=1 node node_modules/@playwright/test/cli.js test --config playwright.review.config.ts
```

The configuration builds and serves the fixture app on `127.0.0.1:3207` and can
reuse an already running build. Use a production build for history validation;
development hot reload during source edits caused a transient blank Forward
navigation that did not reproduce in the clean production build.

Inline Codex browser inspection exercised the actual delivery blocker, exact
finding and highlighted file line, a shared second tab, Back/Forward/reload,
the unavailable-version message, and experiment verdict selection. Browser
verification uses synthetic data and simulated sign-off/rerun responses; it does
not establish deployed authentication, real provider execution, or production
artifact availability. The production fixture build reports an OpenTelemetry
dynamic-require warning from the existing browser observability dependency.
