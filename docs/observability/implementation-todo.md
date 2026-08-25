# Logfire operations metrics implementation

This checklist tracks the first Oddish operations dashboard change. Logfire
owns metric aggregation and graphs; `frontend/` receives no charts, polling,
metric state, hooks, or Context in this change.

- [x] Create an isolated worktree and branch.
- [x] Define metric names, units, meanings, and bounded dimensions.
- [x] Add direct, lazy, failure-isolated metric recording functions.
- [x] Return the accepted `WorkerJobStatus` from `_record_outcome`.
- [x] Emit worker transition and duration observations after the database update.
- [x] Emit `DispatchPlan` queue and slot snapshots in both dispatcher hosts.
- [x] Emit successful spawn counts and dispatcher-cycle outcomes in both hosts.
- [x] Add focused worker, dispatcher, and recorder tests.
- [ ] Export the `Oddish Operations` Logfire dashboard JSON.
- [x] Document standard dashboard enablement, custom panels, variables, and import.
- [x] Run targeted Python tests and lint checks.
- [x] Confirm `frontend/` has no changes.
