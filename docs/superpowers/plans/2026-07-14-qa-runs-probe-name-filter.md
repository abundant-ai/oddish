# QA Runs Probe-Name Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a multi-select probe-name filter to `oddish.app/qa/runs` so users can narrow the task list to tasks that ran a given probe preset.

**Architecture:** Backend `list_org_probes_core` gains a per-task `array_agg` of distinct non-null `harbor_config['probe_name']` values, surfaced as `probe_names: list[str]` on `OrgProbeRow`. The frontend keeps its full-list fetch and client-side filtering: a new multi-select dropdown (Popover + Checkbox, built inline) lets the user pick names; a task row survives if its `probe_names` intersects the selection (any-match), AND-combined with the existing task-name search.

**Tech Stack:** Python / SQLAlchemy (async) + pydantic on the backend; Next.js / React / TypeScript with SWR and Radix-based UI primitives on the frontend.

## Global Constraints

- Never commit to `main`. Work is on branch `qa-runs-probe-name-filter`.
- Filtering stays **client-side** — no new query params on the backend `/probes` endpoint or the `/api/probes` proxy route.
- Aggregated `run_count` / `last_status` / `last_run_at` stay **task-wide** — they are NOT recomputed for the selected probe name.
- `probe_name` lives only in `harbor_config` JSONB (key `probe_name`); it is nullable. Nulls are excluded from `probe_names`.
- Match existing file conventions: pydantic `BaseModel` for schema rows, local-`useState` + `useMemo` client filtering for the page.
- Backend tests run against the real local Postgres: `set -a && source .env && set +a && uv run pytest ...` from `backend/`.

---

### Task 1: Backend — aggregate distinct probe names per task

**Files:**
- Modify: `oddish/src/oddish/schemas.py:1702-1714` (`OrgProbeRow`)
- Modify: `oddish/src/oddish/core/experiments.py:89-151` (`list_org_probes_core`)
- Test: `backend/tests/test_org_probes_api.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `OrgProbeRow.probe_names: list[str]` — distinct, non-null probe names for the task, order unspecified. `list_org_probes_core(session, *, org_id)` return shape unchanged except each row now carries `probe_names`.

- [ ] **Step 1: Write the failing test**

Add this test to `backend/tests/test_org_probes_api.py`. It extends the existing `probed_org` fixture's task A (which already has probe trials `a_old` and `a_new`) by relying on new `harbor_config` values — so first update the fixture's `trial()` helper and the two task-A probe trials to carry probe names, then assert on the aggregate.

First, edit the `trial` helper in the fixture (around line 85) to accept an optional `harbor_config`:

```python
    def trial(
        tid, task_id, oid, eid, created, *, is_probe,
        status=TrialStatus.SUCCESS, harbor_config=None,
    ):
        return TrialModel(
            id=tid,
            name=f"{task_id}-{tid}",
            task_id=task_id,
            experiment_id=eid,
            org_id=oid,
            agent="claude-code",
            provider="anthropic",
            model="anthropic/claude-sonnet-4-6",
            queue_key="test-op",
            status=status,
            origin=TrialOrigin.ODDISH,
            is_probe=is_probe,
            created_at=created,
            harbor_config=harbor_config,
        )
```

Then give task A's two probe trials distinct names, and leave `a_old`'s name null-ish to prove nulls/dupes are handled. Replace the `a_old` / `a_new` `session.add(...)` calls (lines 129-140) with:

```python
        session.add(
            trial(
                a_old, task_a, org_id, exp_id, base, is_probe=True,
                harbor_config={"mode": "probe", "probe_name": "cheat-detection"},
            )
        )
        session.add(
            trial(
                a_new, task_a, org_id, exp_id, base + timedelta(hours=2),
                is_probe=True, status=TrialStatus.RUNNING,
                harbor_config={"mode": "probe", "probe_name": "prompt-injection"},
            )
        )
```

And give task B's probe a name that duplicates one of A's plus a second probe with no name, to prove dedup + null-exclusion on a single task. Replace the `b_probe` block (lines 151-160) with:

```python
        session.add(
            trial(
                b_probe, task_b, org_id, exp_id, base - timedelta(hours=5),
                is_probe=True,
                harbor_config={"mode": "probe", "probe_name": "cheat-detection"},
            )
        )
        b_probe_2 = f"trial_b2_{suffix}"
        session.add(
            trial(
                b_probe_2, task_b, org_id, exp_id, base - timedelta(hours=6),
                is_probe=True,
                harbor_config={"mode": "probe"},  # no probe_name → excluded
            )
        )
```

Add `b_probe_2` to the fixture's `_cleanup(trial_ids=[...])` list (line 171).

Note: task B's `run_count` is now `2` (two probe trials). Update the existing assertion `assert b.run_count == 1` at line 190 to `assert b.run_count == 2`.

Now add the new test at the end of the file:

```python
@pytest.mark.asyncio
async def test_list_org_probes_aggregates_probe_names(probed_org):
    async with get_session() as session:
        rows = await list_org_probes_core(session, org_id=probed_org["org_id"])

    by_task = {r.task_id: r for r in rows}
    a = by_task[probed_org["task_a"]]
    b = by_task[probed_org["task_b"]]

    # Task A: two probe trials with two distinct names.
    assert sorted(a.probe_names) == ["cheat-detection", "prompt-injection"]
    # Task B: one named probe + one unnamed → only the name, no null, no dup.
    assert b.probe_names == ["cheat-detection"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && set -a && source .env && set +a && uv run pytest tests/test_org_probes_api.py -v`
Expected: `test_list_org_probes_aggregates_probe_names` FAILS with `AttributeError: 'OrgProbeRow' object has no attribute 'probe_names'` (and the `b.run_count == 2` assertion now drives the fixture change).

- [ ] **Step 3: Add the field to the schema**

In `oddish/src/oddish/schemas.py`, add to `OrgProbeRow` (after `last_status`, line 1714):

```python
    last_status: str
    probe_names: list[str] = Field(default_factory=list)
```

Ensure `Field` is imported at the top of `schemas.py` (it uses pydantic; if `Field` is not already imported, add it to the existing `from pydantic import ...` line). If the file already imports `Field`, no change needed.

- [ ] **Step 4: Aggregate probe names in the core query**

In `oddish/src/oddish/core/experiments.py`, update `list_org_probes_core`. Add a distinct-array aggregate window to the `ranked_select` (after the `run_count` window, line 116) and project it through.

Add this import near the top of the file if not present: `from sqlalchemy import func` is already used; also need `distinct` and `text` — add `distinct` to the existing `from sqlalchemy import ...` import. Then:

Add to `ranked_select`'s column list (inside the `select(...)` at lines 112-123), after the `run_count` label:

```python
        func.array_agg(distinct(TrialModel.harbor_config["probe_name"].astext))
        .filter(TrialModel.harbor_config["probe_name"].astext.isnot(None))
        .over(partition_by=TrialModel.task_id)
        .label("probe_names"),
```

Project it in the outer `stmt` select (lines 129-135), after `ranked.c.last_status`:

```python
            ranked.c.probe_names,
```

Construct it on the row (lines 143-149), after `last_status=...`:

```python
            probe_names=list(row.probe_names or []),
```

Note on the JSONB accessor: `TrialModel.harbor_config["probe_name"].astext` yields the text value; `.filter(...)` on the aggregate excludes rows where it is SQL NULL, and `distinct(...)` dedupes. `array_agg` over an empty filtered set returns SQL NULL → the `or []` guard yields `[]`.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && set -a && source .env && set +a && uv run pytest tests/test_org_probes_api.py -v`
Expected: both `test_list_org_probes_groups_counts_and_orders` and `test_list_org_probes_aggregates_probe_names` PASS.

- [ ] **Step 6: Commit**

```bash
git add oddish/src/oddish/schemas.py oddish/src/oddish/core/experiments.py backend/tests/test_org_probes_api.py
git commit -m "feat(qa): aggregate distinct probe names per task in org probes query

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Frontend — multi-select probe-name filter

**Files:**
- Modify: `frontend/src/app/(app)/qa/runs/qa-runs-client.tsx`

**Interfaces:**
- Consumes: `OrgProbeRow.probe_names` from Task 1, delivered through `/api/probes` (proxy passes the backend JSON through unchanged) as `ProbeRow.probe_names: string[]`.
- Produces: nothing downstream.

No test suite is wired up for the frontend, so this task is manual-verification only (see Step 6).

- [ ] **Step 1: Extend the `ProbeRow` type**

In `qa-runs-client.tsx`, add `probe_names` to the type (line 21-27):

```ts
type ProbeRow = {
  task_id: string;
  task_name: string;
  run_count: number;
  last_run_at: string | null;
  last_status: string;
  probe_names: string[];
};
```

- [ ] **Step 2: Add imports for the dropdown primitives**

Add these imports alongside the existing UI imports at the top of the file:

```ts
import { ChevronDown } from "lucide-react";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
```

- [ ] **Step 3: Add a self-contained `ProbeNameFilter` component**

Add this component to the file, above `QaRunsClient` (e.g. after the `useDebouncedValue` helper, before line 45). It is a small multi-select mirroring the Tasks sidebar's `MultiSelect` visual style but decoupled from `FilterValues`:

```tsx
function ProbeNameFilter({
  options,
  selected,
  onChange,
}: {
  options: string[];
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  const toggle = (value: string) => {
    onChange(
      selected.includes(value)
        ? selected.filter((v) => v !== value)
        : [...selected, value],
    );
  };
  const label =
    selected.length === 0
      ? "All probes"
      : selected.length === 1
        ? selected[0]
        : `${selected.length} probes`;

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className="h-8 w-full justify-between border-[#6f88b4]/20 text-xs font-normal sm:w-[220px]"
        >
          <span className="truncate">{label}</span>
          <ChevronDown className="h-3.5 w-3.5 opacity-60" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="z-30 w-56 p-2">
        <div className="max-h-56 space-y-0.5 overflow-auto">
          {options.length === 0 ? (
            <p className="text-muted-foreground px-1 py-2 text-xs">
              No probe names
            </p>
          ) : (
            options.map((name) => (
              <label
                key={name}
                className="hover:bg-muted/60 flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-xs"
              >
                <Checkbox
                  checked={selected.includes(name)}
                  onCheckedChange={() => toggle(name)}
                />
                <span className="truncate">{name}</span>
              </label>
            ))
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}
```

- [ ] **Step 4: Wire filter state, options, and the combined filter**

In `QaRunsClient`, add selection state next to `searchQuery` (line 46):

```ts
  const [selectedProbes, setSelectedProbes] = useState<string[]>([]);
```

Reset the page when the selection changes — extend the existing effect (lines 54-56) so its dependency array includes the selection. Replace it with:

```ts
  useEffect(() => {
    setPage(0);
  }, [debouncedQuery, selectedProbes]);
```

Compute the sorted union of probe names for the dropdown options, after the `useSWR` call:

```ts
  const probeOptions = useMemo(() => {
    const names = new Set<string>();
    for (const row of data ?? []) {
      for (const name of row.probe_names ?? []) names.add(name);
    }
    return Array.from(names).sort((a, b) => a.localeCompare(b));
  }, [data]);
```

Extend the `filtered` `useMemo` (lines 58-64) to AND the two filters:

```ts
  const filtered = useMemo(() => {
    const rows = data ?? [];
    return rows.filter((row) => {
      const matchesName =
        !debouncedQuery ||
        row.task_name.toLowerCase().includes(debouncedQuery);
      const matchesProbe =
        selectedProbes.length === 0 ||
        (row.probe_names ?? []).some((n) => selectedProbes.includes(n));
      return matchesName && matchesProbe;
    });
  }, [data, debouncedQuery, selectedProbes]);
```

- [ ] **Step 5: Render the dropdown in the CardHeader**

In the filter container `<div className="flex flex-col gap-2 sm:flex-row sm:items-center">` (line 83), add the `ProbeNameFilter` after the existing `<Input>` (after line 89):

```tsx
            <ProbeNameFilter
              options={probeOptions}
              selected={selectedProbes}
              onChange={setSelectedProbes}
            />
```

Also update the empty-state copy for when filters match nothing (line 110) to be filter-agnostic:

```tsx
              No tasks match the current filters.
```

- [ ] **Step 6: Manual verification**

Run the frontend and backend locally (`cd frontend && pnpm dev`; backend per `backend/README.md`), or verify against a deployed preview. On `/qa/runs`:
- Confirm the "All probes" dropdown lists the distinct probe names present in the data.
- Select one name → only tasks whose probe runs include that name remain; `Runs` / `Last status` / `Last run` columns are unchanged (task-wide).
- Select a second name → tasks matching either name appear (any-match).
- Combine with the task search box → both narrow together (AND).
- Clear the selection → all tasks return.

Also run the typecheck/lint the repo uses:

Run: `cd frontend && pnpm lint`
Expected: no new errors from `qa-runs-client.tsx`.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/app/\(app\)/qa/runs/qa-runs-client.tsx
git commit -m "feat(qa): filter /qa/runs by probe name

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

- **Spec coverage:** backend aggregation of distinct non-null probe names (Task 1); `OrgProbeRow.probe_names` (Task 1); `ProbeRow.probe_names` + multi-select dropdown + any-match + AND-with-search + page reset + task-wide counts unchanged (Task 2); test extending `test_org_probes_api.py` (Task 1). Proxy route intentionally untouched (documented in Global Constraints). All spec sections covered.
- **Placeholders:** none — all steps carry concrete code and commands.
- **Type consistency:** `probe_names: list[str]` (Python) ↔ `probe_names: string[]` (TS); `ProbeNameFilter` props (`options`/`selected`/`onChange`) match the call site; `selectedProbes`/`setSelectedProbes` consistent throughout.
