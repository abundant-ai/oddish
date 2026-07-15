# Filter `/qa/runs` by probe name

## Goal

On `oddish.app/qa/runs`, let the user filter the list of probe runs by probe
name (e.g. a preset like `cheat-detection`), in addition to the existing
task-name text search.

## Background

- `/qa/runs` (`frontend/src/app/(app)/qa/runs/qa-runs-client.tsx`) shows **one
  row per task** that has at least one probe trial. Each row aggregates: total
  probe-run count, and the status/time of the most recent probe trial.
- A "probe" is a `TrialModel` row with `is_probe = true`. Its human-readable
  name lives in the `harbor_config` JSONB blob under `probe_name`
  (`oddish/src/oddish/schemas.py:324`). It is nullable (falls back to the agent
  name in other UIs).
- Because the page is aggregated per task, a single task can have multiple
  probe runs with **different** probe names.
- The page fetches the full list from `/api/probes` → backend `GET /probes`
  (`backend/api/routers/tasks.py:1258`, core
  `list_org_probes_core` at `oddish/src/oddish/core/experiments.py:89`) and does
  all filtering/pagination **client-side**.
- "Probe type" collapses to probe name on this page: every row is already
  `is_probe` / `harbor_config.mode == "probe"`, so there is no other type axis
  to filter on.

## Decisions

- **Semantics:** keep one-row-per-task. A task appears if **any** of its probe
  runs has a selected probe name (any-match). `run_count` / `last_status` /
  `last_run_at` stay task-wide — they are **not** recomputed for the selected
  probe name.
- **Control:** a **multi-select dropdown** whose options are the distinct probe
  names present in the fetched data, sorted. No selection = show all.
- **Filtering stays client-side**, matching the current page architecture (no
  new query params on the backend or proxy route).

## Design

### Backend

`list_org_probes_core` (`oddish/src/oddish/core/experiments.py:89`) already
produces one row per task. Add a per-task aggregation of distinct, non-null
probe names:

- Aggregate `harbor_config['probe_name']` (as text) with
  `array_agg(distinct ...)`, filtered to non-null values, grouped per task.
- Expose it as a new field on the row.

`OrgProbeRow` (`oddish/src/oddish/schemas.py:1702`) gains:

```python
probe_names: list[str] = field(default_factory=list)
```

(Match the schema's existing style — dataclass/pydantic as used by the
surrounding rows.)

### Frontend

- `ProbeRow` type in `qa-runs-client.tsx` gains `probe_names: string[]`.
- The `/api/probes` proxy route (`frontend/src/app/api/probes/route.ts`) is
  unchanged — still returns the full list.
- In `qa-runs-client.tsx`:
  - Add a multi-select dropdown in the CardHeader next to the existing
    task-name search `<Input>`. Options = sorted union of all `probe_names`
    across fetched rows.
  - Local `useState` for the selected probe names (matching the existing
    debounced-search local-state pattern; no URL params).
  - Extend the `useMemo` filter: a row passes if
    `selected.length === 0 || row.probe_names.some(n => selected.includes(n))`.
    Combine with the existing task-name filter via AND.
  - Reset page to 0 when the selected set changes (existing pattern for the
    search input).
  - Reuse the multi-select component already used by the Tasks filter sidebar
    (`frontend/src/components/ui/select.tsx` / the `MultiSelect` used in
    `tasks-filter-sidebar.tsx`) rather than hand-rolling one.

## Testing

- Extend `backend/tests/test_org_probes_api.py`: create a task with multiple
  probe trials of different `probe_name` values (and at least one null), assert
  the returned row's `probe_names` contains the distinct non-null names and no
  duplicates/nulls.
- Frontend: no test suite is wired up; verify manually that selecting one or
  more probe names narrows the list to matching tasks and clearing shows all.

## Out of scope

- Recomputing counts/last-run per selected probe name.
- Switching the page to one-row-per-run.
- Server-side / URL-shareable filter params.
