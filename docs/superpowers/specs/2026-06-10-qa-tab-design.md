# QA tab — design

## Problem

Today the dashboard top nav is `Dashboard · Tasks · Skills · Documents`. Skills and
Documents are standalone CRUD pages. Probe **presets** have no page of their own — they
exist only as a modal inside the probe submit form. Probe **runs** are reachable only
per-task at `/tasks/{id}/probe` (a submit form + a history table scoped to that one task).

These are all facets of one workflow — QA-ing tasks with probe agents — but they're
scattered. We want a single **QA** tab that gathers them: the configs (skills, documents,
presets) that probe agents draw on, plus a listing of probe runs across tasks.

## Goals

- Add a top-level **QA** tab next to Tasks; remove the Skills and Documents tabs.
- Group skills, documents, and probe presets under QA as **Configs**.
- List probe runs in QA, grouped at the task level (tasks that have runs, most-recent first).
- Keep existing per-task probe launch + history working; don't rebuild it.

## Non-goals

- No change to how a probe run actually executes, or to the probe submit form itself.
- No change to the skills/documents/presets data models or their CRUD endpoints.
- No redesign of the per-task probe page (`/tasks/{id}/probe`).

## Information architecture

Top nav becomes: **Dashboard · Tasks · QA** (QA icon: `FlaskConical` from lucide-react).

Nested routes under `/qa`, with a shared layout rendering a two-group sub-nav:

```
/qa                 → redirect to /qa/runs
/qa/runs            → Probe Runs  (group 1)
/qa/skills          → Configs › Skills     (group 2)
/qa/documents       → Configs › Documents  (group 2)
/qa/presets         → Configs › Presets    (group 2)
```

Sub-nav structure (group B from brainstorming): two top groups, **Probe Runs** | **Configs**.
The Configs group exposes inner tabs: Skills · Documents · Presets. Active state derives
from `usePathname()`.

Old routes redirect (so existing bookmarks/links keep working):

```
/skills      → /qa/skills
/documents   → /qa/documents
```

## Components & files

### Frontend

- `frontend/src/components/nav.tsx` — replace the Skills and Documents nav buttons with a
  single **QA** button linking to `/qa`. Active when `pathname` starts with `/qa`.
- `frontend/src/app/(app)/qa/layout.tsx` — QA shell: renders the two-group sub-nav
  (Probe Runs | Configs, with Configs' inner Skills/Documents/Presets tabs) above
  `{children}`. Active styling matches the existing nav button pattern
  (`data-active` + `border-[#85b85c]/25`).
- `frontend/src/app/(app)/qa/page.tsx` — redirects to `/qa/runs` (`redirect()` from
  `next/navigation`).
- `frontend/src/app/(app)/qa/runs/page.tsx` + `qa-runs-client.tsx` — the Probe Runs view.
- `frontend/src/app/(app)/qa/skills/page.tsx` — renders the existing `<SkillsClient />`.
- `frontend/src/app/(app)/qa/documents/page.tsx` — renders the existing `<DocumentsClient />`.
- `frontend/src/app/(app)/qa/presets/page.tsx` + `presets-client.tsx` — new full
  management page for probe presets.
- Move the client components to live under `qa/`:
  - `skills/skills-client.tsx` → `qa/skills/skills-client.tsx`
  - `documents/documents-client.tsx` → `qa/documents/documents-client.tsx`
  (Their internals are self-contained — only import paths change.)
- `frontend/src/app/(app)/skills/page.tsx` and `.../documents/page.tsx` — replace with
  `redirect("/qa/skills")` / `redirect("/qa/documents")`; delete the moved client files
  from those folders.
- `frontend/src/app/api/probes/route.ts` — new Next proxy → backend `GET /probes`.

### Probe Runs view (`/qa/runs`)

- Fetches `/api/probes` → list of `{ task_id, task_name, run_count, last_run_at,
  last_status }`, already ordered most-recent-first by the backend.
- Renders one table row per task: name, run count, last-run relative time, last-run status
  badge. The row links to `/tasks/{task_id}/probe` (the existing per-task probe page with
  its submit form + full history table).
- Empty state mirrors Skills/Documents ("No probe runs yet").
- **+ New probe run** button (top-right, like "+ New skill"): opens a task picker — a
  searchable list backed by the existing `/api/tasks`. Selecting a task navigates to
  `/tasks/{task_id}/probe`. No new probe-launch UI is built; the existing submit form is
  the launch surface.

### Presets management page (`/qa/presets`)

Mirrors the Skills page structure (`skills-client.tsx`): a list with create / edit / delete,
plus a create/edit form. Backed by the existing endpoints:

- `GET /api/probe-presets` — list (global seeds + org's custom presets)
- `POST /api/probe-presets` — create (also used to fork a seed)
- `PUT /api/probe-presets/{id}` — update a custom preset
- `DELETE /api/probe-presets/{id}` — delete a custom preset

Seed presets are read-only (no edit/delete), shown with a "Seed" badge — same convention
as seed skills. Preset fields surfaced in the form: name, agent, model, operator prompt,
result focus, evaluation metric, ratio unit, ratio verb (the fields already handled by the
submit-form modal in `probe-submit-form.tsx`). The submit form's inline preset picker is
unchanged and reads from the same endpoints.

### Backend

No existing query returns org-wide tasks-with-probe-runs (only per-experiment, via
`list_experiment_probes_core` in `oddish/src/oddish/core/experiments.py`). Add:

- `list_org_probes_core(session, *, org_id) -> list[OrgProbeRow]` in
  `oddish/src/oddish/core/experiments.py` (or a sibling core module). Aggregates
  `TrialModel` rows where `is_probe is True`, grouped by task, returning per task:
  `task_id`, `task_name`, `run_count`, `last_run_at` (max `created_at`),
  `last_status` (status of the most-recent probe trial). Ordered by `last_run_at desc`.
  Org-scoped via `TaskModel.org_id == org_id` (pass `None` in single-tenant/OSS).
- `OrgProbeRow` Pydantic schema in `oddish/src/oddish/schemas.py`.
- `GET /probes` route in `backend/api/routers/tasks.py` (or `trials.py`) →
  `list_org_probes_core`, returning `list[OrgProbeRow]`.

## Data flow

```
/qa/runs  ──GET /api/probes──▶  Next proxy ──▶  backend GET /probes
                                                   └─ list_org_probes_core(org_id)
                                                        └─ TrialModel where is_probe,
                                                           grouped by task, ordered by
                                                           last_run_at desc
        ◀── [{task_id, task_name, run_count, last_run_at, last_status}, …] ──┘

row click ──▶ /tasks/{task_id}/probe   (existing submit form + ProbeHistoryTable)
+ New run  ──▶ task picker (/api/tasks) ──▶ /tasks/{task_id}/probe
```

## Error handling

- `/qa/runs`: on fetch failure, show the same destructive `Alert` pattern used by
  Skills/Documents ("Failed to load probe runs"). Loading and empty states match those pages.
- Presets page: reuse the create/edit/delete error handling from `skills-client.tsx`
  (inline `Alert` in the form, `alert()` on delete failure) for consistency.
- `/api/probes` proxy: mirror the existing proxy error contract (401 unauthorized,
  503 on upstream/network error) used by `/api/probe-presets/route.ts`.

## Testing

- Backend: unit-test `list_org_probes_core` like the existing
  `tests/test_experiment_probes_api.py` — seed tasks with/without probe trials across two
  orgs, assert grouping, run-count, last-run ordering, and org isolation.
- Frontend: no test suite is wired up; verify manually that nav, redirects, runs list,
  task picker, and presets CRUD work.

## Open questions

None — all resolved during brainstorming.
