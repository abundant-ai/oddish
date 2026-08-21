# QA analysis repair and visibility

Implementation checklist for making task-scoped QA trials repair invalid output
before Harbor verification and making the QA run's files discoverable from the
task page.

## Agent lifecycle

- [x] Verify the installed Harbor Claude agent uses `--continue` against its
      persisted sandbox session and cover that path with wrapper tests.
- [x] Give QA/audit trials an analysis-specific Claude Code wrapper instead of
      the probe wrapper.
- [x] Stage one executable validation command backed by the existing
      `analysis_result_check.py`; do not create a second schema implementation.
- [x] Validate `/logs/qa_result.json` after the first QA response.
- [x] On failure, preserve the attempted JSON and validator output, then inject
      a repair-only prompt containing the exact validation errors.
- [x] Bound repair to two attempts and keep the final Harbor verifier as the
      authority that accepts or rejects the artifact.

## Failure and timeout behavior

- [x] Stop applying the 1,800-second probe timeout cap to QA/audit trials.
- [x] Preserve the task/version timeout contract for analysis trials.
- [x] When a failed QA trial uploaded `qa_result.json`, surface its exact schema
      violations without importing any partial classifications or verdict.
- [x] Correct comments and tests that claim a missing reward automatically
      retries when Harbor excludes that exception.

## QA run API

- [x] Add an org-scoped task endpoint listing real `kind="qa"` trials without
      changing the agent-only task-trials endpoint.
- [x] Include status, model/provider, time/cost, error, and output availability.
- [x] Add org-scoped reads for generated QA JSON and validator output using the
      existing trial artifact/S3 resolution path.
- [x] Cover cross-organization access, missing files, active runs, failed runs,
      and superseded runs.

## Task UI

- [x] Rename frontend variables that call analyzed agent trials `qaTrials`.
- [x] Add one `QaRunsPanel` that owns the QA-runs SWR request and polling.
- [x] Derive latest run, status, and available actions during render; do not
      mirror API data through local state or Effects.
- [x] Use stable trial IDs as row keys; output/log actions open their stable API
      resources directly and require no duplicated local state.
- [x] Link each QA run to its existing trial trace and expose generated JSON,
      validation errors, and logs when present.
- [x] Add component/resource tests for terminal and active QA runs.

## Verification

- [x] Unit-test valid-first-pass, repair-success, and permanently-invalid agent
      output.
- [x] Run targeted Oddish and backend pytest suites.
- [x] Run frontend unit tests, type checking, and lint for touched files.
- [x] Record final diff statistics and any unverified integration behavior.
- [ ] After deploy, run one real QA trial that needs repair and confirm the
      provider session resumes; this spends model credits and is not a local
      test.

## Local verification record

- Diff: 1,296 additions and 113 deletions (net +1,183). Production code is
  net +692; tests are net +398; this checklist and architecture notes are
  net +93.
- Python: 44 selected QA/analysis/API tests passed; 19 database-dependent tests
  skipped because `ODDISH_DATABASE_URL` was not set.
- Frontend: TypeScript and ESLint passed; 13 resource tests passed; Prettier
  matched every touched frontend file.
- The whole `test_harbor_runner.py` file has 152 passing tests and two failures.
  Both failures reproduce unchanged in the clean staging worktree: Cursor web
  tool flags are empty, and a preflight test's fake agent lacks `kwargs`.
- Not locally verified: a deployed provider call that resumes an actual Claude
  Code session after the first model response.
