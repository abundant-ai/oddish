# Grouped Trajectory Steps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fold the real, expandable trajectory step rows under their taxonomy component headers in the Trajectory card, and delete the Activity card's duplicate thin step list.

**Architecture:** A new pure module (`lib/trajectory-segments.ts`) normalizes the summary's `components` into segments and groups indexed steps under them. A shared SWR hook gives all three Summary-tab components one request for the summary. The Trajectory card renders group headers interleaved inside its **existing** single `<Accordion>`, so expand state, refs, and deep links keep working on flat array indices.

**Tech Stack:** Next.js 15 (App Router), React, TypeScript, SWR, Radix Accordion, Tailwind, pnpm.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-20-grouped-trajectory-steps-design.md`.
- Branch off `origin/main` (currently `5fc8fd13`). **Never commit to `main`** — feature branch + PR.
- **No new dependencies.** All deps are exact-pinned for supply-chain reasons (see the `"//"` note in `frontend/package.json`).
- **No Claude co-author or "Generated with" trailers** in commit messages.
- Working directory for all `pnpm` commands: `frontend/`.
- Build verification is `pnpm run build`, never `tsc --noEmit` alone — a partial `node_modules` has masked real resolution errors before. If the build reports missing modules, run `pnpm install --frozen-lockfile` first.
- The frontend has **no unit-test runner**. Task 1's tests run via Node's built-in runner on a throwaway file outside the repo; they are verification, not committed test infrastructure.
- Scratchpad (throwaway files, never committed):
  `/private/tmp/claude-501/-Users-kateyeh-Developer-os-repos-oddish-cutover/7494e420-eb61-494b-ba8b-269e1fb8021d/scratchpad`

## File Structure

| File | Responsibility |
|---|---|
| `frontend/src/lib/trajectory-segments.ts` | **New.** Pure. Taxonomy display labels, summary→segments normalization, step grouping, range labels. No React, no I/O. |
| `frontend/src/lib/use-trajectory-summary.ts` | **New.** The one SWR handle for a trial's trajectory summary. |
| `frontend/src/lib/trajectory-metrics.ts` | Loses `componentLabel`/`COMPONENT_LABELS` (moved). Keeps `phaseColorVars`, `stepDurationsMs`, `stepTokens`. |
| `frontend/src/components/trajectory-viewer.tsx` | Renders the grouped accordion. Owns search, expand state, refs, deep links. |
| `frontend/src/components/trajectory-activity.tsx` | Visuals only: legend, timeline, token heatmap. Loses its step list. |
| `frontend/src/components/trajectory-summary.tsx` | Uses the shared hook. |

---

### Task 1: Pure segments + grouping module

**Files:**
- Create: `frontend/src/lib/trajectory-segments.ts`
- Modify: `frontend/src/lib/trajectory-metrics.ts` (delete `COMPONENT_LABELS` + `componentLabel`, and the now-unused `TrajectoryComponentKind` import)
- Modify: `frontend/src/components/trajectory-activity.tsx` (repoint the `componentLabel` import; delete its local `Segment` + `toSegments`)
- Test: `<scratchpad>/trajectory-segments.test.ts` (throwaway, not committed)

**Interfaces:**
- Consumes: `TrajectoryComponentKind`, `TrajectoryStep`, `TrajectorySummary` from `@/lib/types` (all already exist on `main` as of #832).
- Produces:
  - `interface Segment { key: string; label: string; gist: string; stepIds: number[] }`
  - `interface IndexedStep { step: TrajectoryStep; idx: number }`
  - `interface StepGroup { key: string | null; label: string | null; gist: string; steps: IndexedStep[] }`
  - `componentLabel(kind: string): string`
  - `toSegments(summary: TrajectorySummary | null | undefined): Segment[]`
  - `groupStepsBySegment(steps: IndexedStep[], segments: Segment[]): StepGroup[]`
  - `stepRangeLabel(group: StepGroup): string`

- [ ] **Step 1: Create the branch**

```bash
cd /Users/kateyeh/Developer/os_repos/oddish-cutover
git fetch origin
git checkout -b feat/grouped-trajectory-steps origin/main
```

- [ ] **Step 2: Write the failing test**

Create `<scratchpad>/trajectory-segments.test.ts`. Note the absolute import specifier — the scratchpad is outside the repo, so `@/`-aliases do not resolve there; the module under test only uses `@/lib/types` in a type-only import, which Node erases.

```ts
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  componentLabel,
  groupStepsBySegment,
  stepRangeLabel,
  toSegments,
  type IndexedStep,
  type Segment,
} from "/Users/kateyeh/Developer/os_repos/oddish-cutover/frontend/src/lib/trajectory-segments.ts";

// Minimal stand-ins: grouping only ever reads step_id.
const mkSteps = (ids: number[]): IndexedStep[] =>
  ids.map((id, idx) => ({ step: { step_id: id } as never, idx }));

const seg = (key: string, stepIds: number[]): Segment => ({
  key,
  label: componentLabel(key),
  gist: "",
  stepIds,
});

const shape = (groups: ReturnType<typeof groupStepsBySegment>) =>
  groups.map((g) => [g.key, g.steps.map((s) => s.step.step_id)]);

test("componentLabel maps taxonomy values and degrades for unknowns", () => {
  assert.equal(componentLabel("testing_custom_edge_cases"), "Testing edge cases");
  assert.equal(componentLabel("some_future_value"), "some future value");
});

test("toSegments prefers components and drops empty ones", () => {
  const out = toSegments({
    schema_version: "4",
    model: "m",
    generated_at: "t",
    summary: "s",
    highlights: [],
    components: [
      { step_ids: [2, 1], trajectory_component: "reading_files", summary: "explored" },
      { step_ids: [], trajectory_component: "debugging", summary: null },
    ],
  } as never);
  assert.equal(out.length, 1);
  assert.equal(out[0].key, "reading_files");
  assert.equal(out[0].label, "Reading files");
  assert.equal(out[0].gist, "explored");
  assert.deepEqual(out[0].stepIds, [1, 2]); // sorted
});

test("toSegments falls back to legacy phases", () => {
  const out = toSegments({
    schema_version: "3",
    model: "m",
    generated_at: "t",
    summary: "s",
    highlights: [],
    phases: [{ label: "Explore", gist: "looked around", step_ids: [1, 2] }],
  } as never);
  assert.deepEqual(shape(groupStepsBySegment(mkSteps([1, 2]), out)), [["Explore", [1, 2]]]);
});

test("contiguous segments become one group each", () => {
  const groups = groupStepsBySegment(mkSteps([1, 2, 3, 4]), [
    seg("reading_files", [1, 2]),
    seg("debugging", [3, 4]),
  ]);
  assert.deepEqual(shape(groups), [
    ["reading_files", [1, 2]],
    ["debugging", [3, 4]],
  ]);
  assert.equal(groups[0].label, "Reading files");
});

test("unclaimed steps form an unlabeled run in place", () => {
  const groups = groupStepsBySegment(mkSteps([1, 2, 3, 4, 5]), [
    seg("reading_files", [1, 2]),
    seg("debugging", [5]),
  ]);
  assert.deepEqual(shape(groups), [
    ["reading_files", [1, 2]],
    [null, [3, 4]],
    ["debugging", [5]],
  ]);
  assert.equal(groups[1].label, null);
});

test("interleaved segments produce separate runs sharing a key", () => {
  const groups = groupStepsBySegment(mkSteps([1, 2, 3, 4, 5]), [
    seg("reading_files", [1, 2, 5]),
    seg("debugging", [3, 4]),
  ]);
  assert.deepEqual(shape(groups), [
    ["reading_files", [1, 2]],
    ["debugging", [3, 4]],
    ["reading_files", [5]],
  ]);
});

test("a double-claimed step is rendered once, under the earlier segment", () => {
  const groups = groupStepsBySegment(mkSteps([1, 2, 3]), [
    seg("debugging", [2, 3]),
    seg("reading_files", [1, 2]),
  ]);
  // Ordered by lowest step_id, so reading_files (1) claims 2 before debugging (2) does.
  assert.deepEqual(shape(groups), [
    ["reading_files", [1, 2]],
    ["debugging", [3]],
  ]);
});

test("no segments yields a single unlabeled group", () => {
  assert.deepEqual(shape(groupStepsBySegment(mkSteps([1, 2]), [])), [[null, [1, 2]]]);
});

test("grouping over a filtered step list omits groups with no survivors", () => {
  const groups = groupStepsBySegment(mkSteps([1, 5]), [
    seg("reading_files", [1, 2]),
    seg("implementing", [3, 4]),
    seg("debugging", [5]),
  ]);
  assert.deepEqual(shape(groups), [
    ["reading_files", [1]],
    ["debugging", [5]],
  ]);
});

test("stepRangeLabel reports the run's own visible span", () => {
  const [g] = groupStepsBySegment(mkSteps([11, 27]), [seg("thinking_recall", [11, 27])]);
  assert.equal(stepRangeLabel(g), "steps 11–27");
  const [one] = groupStepsBySegment(mkSteps([9]), [seg("debugging", [9])]);
  assert.equal(stepRangeLabel(one), "step 9");
});

test("string step_ids from lenient producers still group", () => {
  const steps = [{ step: { step_id: "1" } as never, idx: 0 }];
  assert.deepEqual(shape(groupStepsBySegment(steps, [seg("reading_files", [1])])), [
    ["reading_files", ["1"]],
  ]);
});
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
cd /Users/kateyeh/Developer/os_repos/oddish-cutover
node --test '/private/tmp/claude-501/-Users-kateyeh-Developer-os-repos-oddish-cutover/7494e420-eb61-494b-ba8b-269e1fb8021d/scratchpad/trajectory-segments.test.ts'
```

Expected: FAIL — `Cannot find module .../trajectory-segments.ts`.

- [ ] **Step 4: Write the module**

Create `frontend/src/lib/trajectory-segments.ts`:

```ts
import type {
  TrajectoryComponentKind,
  TrajectoryStep,
  TrajectorySummary,
} from "@/lib/types";

const COMPONENT_LABELS: Record<TrajectoryComponentKind, string> = {
  reading_files: "Reading files",
  thinking_recall: "Recalling",
  thinking_understand: "Understanding",
  thinking_hypothesize: "Hypothesizing",
  thinking_diagnose: "Diagnosing",
  implementing: "Implementing",
  writing_tests: "Writing tests",
  testing_public: "Running public tests",
  testing_custom: "Running custom tests",
  testing_custom_edge_cases: "Testing edge cases",
  debugging: "Debugging",
};

/** Display label for a taxonomy value; unknown values degrade to de-snaked text. */
export function componentLabel(kind: string): string {
  return (
    COMPONENT_LABELS[kind as TrajectoryComponentKind] ?? kind.replace(/_/g, " ")
  );
}

/**
 * Normalized segment of the run, from the summary's `components` (taxonomy-valued,
 * schema v4+) or its legacy free-text `phases`. `key` is what colors are assigned
 * by, so repeats of the same taxonomy value share a color.
 */
export interface Segment {
  key: string;
  label: string;
  gist: string;
  stepIds: number[];
}

/** A step paired with its index in the *full* trajectory, which search must not disturb. */
export interface IndexedStep {
  step: TrajectoryStep;
  idx: number;
}

/** A contiguous run of steps under one segment. `key: null` = claimed by none. */
export interface StepGroup {
  key: string | null;
  label: string | null;
  gist: string;
  steps: IndexedStep[];
}

export function toSegments(
  summary: TrajectorySummary | null | undefined,
): Segment[] {
  if (!summary) return [];
  const sorted = (ids: number[]) => [...ids].sort((a, b) => a - b);
  if (summary.components?.length) {
    return summary.components
      .filter((c) => c.step_ids.length > 0)
      .map((c) => ({
        key: c.trajectory_component,
        label: componentLabel(c.trajectory_component),
        gist: c.summary ?? "",
        stepIds: sorted(c.step_ids),
      }));
  }
  return (summary.phases ?? [])
    .filter((p) => p.step_ids.length > 0)
    .map((p) => ({
      key: p.label,
      label: p.label,
      gist: p.gist,
      stepIds: sorted(p.step_ids),
    }));
}

/**
 * Cut `steps` into contiguous runs by owning segment, preserving step order.
 *
 * Segments are not a partition: the model can leave steps unclaimed, interleave
 * two components, or put one step_id in two components (the backend's
 * `filter_output` drops invalid ids but never dedupes). First claim wins, ordered
 * by lowest step_id, so every step renders exactly once and in true order.
 */
export function groupStepsBySegment(
  steps: IndexedStep[],
  segments: Segment[],
): StepGroup[] {
  const ordered = segments
    .filter((s) => s.stepIds.length > 0)
    .sort((a, b) => Math.min(...a.stepIds) - Math.min(...b.stepIds));

  const owner = new Map<number, Segment>();
  for (const segment of ordered) {
    for (const id of segment.stepIds) {
      if (!owner.has(id)) owner.set(id, segment);
    }
  }

  const groups: StepGroup[] = [];
  let current: StepGroup | null = null;
  let currentSegment: Segment | null = null;
  for (const entry of steps) {
    // step_id is typed number but arrives as a string from some producers.
    const segment = owner.get(Number(entry.step.step_id)) ?? null;
    if (!current || segment !== currentSegment) {
      current = {
        key: segment?.key ?? null,
        label: segment?.label ?? null,
        gist: segment?.gist ?? "",
        steps: [],
      };
      groups.push(current);
      currentSegment = segment;
    }
    current.steps.push(entry);
  }
  return groups;
}

/** Range label for a group, computed from the steps actually shown. */
export function stepRangeLabel(group: StepGroup): string {
  const ids = group.steps.map((s) => Number(s.step.step_id));
  const lo = Math.min(...ids);
  const hi = Math.max(...ids);
  return lo === hi ? `step ${lo}` : `steps ${lo}–${hi}`;
}
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
node --test '/private/tmp/claude-501/-Users-kateyeh-Developer-os-repos-oddish-cutover/7494e420-eb61-494b-ba8b-269e1fb8021d/scratchpad/trajectory-segments.test.ts'
```

Expected: `pass 11`, `fail 0`.

- [ ] **Step 6: Prove the double-claim test is not theater**

A first-claim-wins bug is invisible if the test would pass either way. Temporarily break the rule — in `groupStepsBySegment`, change `if (!owner.has(id)) owner.set(id, segment);` to `owner.set(id, segment);` (last claim wins) — and re-run.

Expected: the "double-claimed step" test FAILS (step 2 lands under `debugging`). Restore the `if (!owner.has(id))` guard and re-run to green before continuing.

- [ ] **Step 7: Remove the moved helpers from `trajectory-metrics.ts`**

Delete the `COMPONENT_LABELS` const and the `componentLabel` function, and restore the import line to:

```ts
import type { TrajectoryStep } from "@/lib/types";
```

`phaseColorVars`, `stepDurationsMs`, and `stepTokens` stay put.

- [ ] **Step 8: Repoint `trajectory-activity.tsx` at the new module**

Delete its local `Segment` interface (lines 15–25) and its `toSegments` function, then set the imports to:

```ts
import { phaseColorVars, stepDurationsMs, stepTokens } from "@/lib/trajectory-metrics";
import { toSegments } from "@/lib/trajectory-segments";
```

`componentLabel` is **not** imported here — its only call site in this file was inside the deleted local `toSegments`. The labels the legend renders come off `Segment.label`, which `toSegments` already resolved.

- [ ] **Step 9: Verify the build**

```bash
cd frontend && pnpm run build
```

Expected: `✓ Compiled successfully`. Also run `pnpm run lint` and `pnpm run dead-code`; knip must not report `trajectory-segments.ts` exports as unused (`stepRangeLabel` and `groupStepsBySegment` are consumed in Task 3 — if knip complains now, note it and re-check after Task 3 rather than deleting anything).

- [ ] **Step 10: Commit**

```bash
cd /Users/kateyeh/Developer/os_repos/oddish-cutover
git add frontend/src/lib/trajectory-segments.ts frontend/src/lib/trajectory-metrics.ts frontend/src/components/trajectory-activity.tsx
git commit -m "refactor(frontend): extract pure trajectory segment + grouping module"
```

---

### Task 2: Shared trajectory-summary hook

**Files:**
- Create: `frontend/src/lib/use-trajectory-summary.ts`
- Modify: `frontend/src/components/trajectory-summary.tsx:28-32`
- Modify: `frontend/src/components/trajectory-activity.tsx:45-50`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `useTrajectorySummary(trialId: string, apiBaseUrl?: string)` returning SWR's handle (`{ data, error, isLoading, mutate }`) over `TrajectorySummary | null`.

No behavior changes in this task — it makes an existing implicit dedup explicit so Task 3 can add a third consumer without a third literal key.

- [ ] **Step 1: Create the hook**

`frontend/src/lib/use-trajectory-summary.ts`:

```ts
"use client";

import useSWR from "swr";
import { fetcher } from "@/lib/api";
import type { TrajectorySummary } from "@/lib/types";

/**
 * The one SWR handle for a trial's trajectory summary. Three components on the
 * Summary tab need it; a single key means a single request.
 */
export function useTrajectorySummary(trialId: string, apiBaseUrl = "/api") {
  return useSWR<TrajectorySummary | null>(
    `${apiBaseUrl}/trials/${trialId}/trajectory/summary`,
    fetcher,
    { revalidateOnFocus: false },
  );
}
```

- [ ] **Step 2: Use it in `trajectory-summary.tsx`**

Replace the `useSWR` call with:

```tsx
const { data, error, isLoading, mutate } = useTrajectorySummary(trialId, apiBaseUrl);
```

Add `import { useTrajectorySummary } from "@/lib/use-trajectory-summary";`, and drop the now-unused `useSWR`, `fetcher`, and `TrajectorySummary as TrajectorySummaryT` imports.

- [ ] **Step 3: Use it in `trajectory-activity.tsx`**

Replace its `useSWR` call (and the "Same SWR key" comment, now redundant) with:

```tsx
const { data } = useTrajectorySummary(trialId, apiBaseUrl);
```

Drop the now-unused `useSWR`, `fetcher`, and `TrajectorySummary` imports.

- [ ] **Step 4: Verify the build and the request count**

```bash
cd frontend && pnpm run build && pnpm run lint
```

Expected: compiles clean, no unused-import lint errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/use-trajectory-summary.ts frontend/src/components/trajectory-summary.tsx frontend/src/components/trajectory-activity.tsx
git commit -m "refactor(frontend): share one SWR handle for the trajectory summary"
```

---

### Task 3: Render the Trajectory accordion in groups

**Files:**
- Modify: `frontend/src/components/trajectory-viewer.tsx` (imports; `TrajectoryViewer` body near `:744`; the Accordion render at `:940-973`)

**Interfaces:**
- Consumes: `toSegments`, `groupStepsBySegment`, `stepRangeLabel` from `@/lib/trajectory-segments` (Task 1); `useTrajectorySummary` from `@/lib/use-trajectory-summary` (Task 2); `phaseColorVars` from `@/lib/trajectory-metrics`.
- Produces: nothing consumed by later tasks.

After this task the page briefly shows the step list twice — grouped here, thin in Activity. Task 4 removes the duplicate.

- [ ] **Step 1: Add the imports**

```ts
import { phaseColorVars } from "@/lib/trajectory-metrics";
import {
  groupStepsBySegment,
  stepRangeLabel,
  toSegments,
} from "@/lib/trajectory-segments";
import { useTrajectorySummary } from "@/lib/use-trajectory-summary";
```

- [ ] **Step 2: Derive the groups**

Immediately after the existing `visibleSteps` memo (`trajectory-viewer.tsx:744-748`), add:

```tsx
  const { data: summary } = useTrajectorySummary(trialId, apiBaseUrl);
  const segments = useMemo(() => toSegments(summary), [summary]);
  const colorFor = useMemo(
    () => phaseColorVars(segments.map((s) => s.key)),
    [segments],
  );
  // Grouping runs over the *filtered* list, so a group whose steps all filtered
  // out is simply never emitted.
  const groups = useMemo(
    () => groupStepsBySegment(visibleSteps, segments),
    [visibleSteps, segments],
  );
```

The `useTrajectorySummary` call must sit with the other hooks, above the `isLoading` / `error` / `!trajectory` early returns — React hooks cannot run conditionally.

- [ ] **Step 3: Render group headers inside the existing Accordion**

Replace the `<Accordion>` block (`:940-973`) with:

```tsx
            <Accordion
              type="multiple"
              value={expandedSteps}
              onValueChange={setExpandedSteps}
            >
              {groups.map((group, gi) => (
                <div key={`${group.key ?? "unclaimed"}-${gi}`} className="mt-5 first:mt-0">
                  {group.label && (
                    <div className="flex items-center gap-2 border-b pb-1.5">
                      <span
                        className="h-4 w-1 rounded-sm"
                        style={{
                          background:
                            colorFor.get(group.key ?? "") ?? "var(--phase-other)",
                        }}
                      />
                      <span className="text-sm font-semibold">{group.label}</span>
                      <span className="ml-auto font-mono text-xs text-muted-foreground">
                        {stepRangeLabel(group)}
                      </span>
                    </div>
                  )}
                  {group.gist && (
                    <p className="pb-1 pt-1.5 text-xs text-muted-foreground">
                      {group.gist}
                    </p>
                  )}
                  {group.steps.map(({ step, idx }) => (
                    <AccordionItem
                      key={step.step_id}
                      value={`step-${idx}`}
                      ref={(el: HTMLDivElement | null) => {
                        stepRefs.current[idx] = el;
                      }}
                    >
                      <AccordionTrigger className="py-3 hover:no-underline">
                        <StepTrigger
                          step={step}
                          prevTimestamp={
                            idx > 0
                              ? (trajectory.steps[idx - 1]?.timestamp ?? null)
                              : null
                          }
                          startTimestamp={trajectory.steps[0]?.timestamp ?? null}
                        />
                      </AccordionTrigger>
                      <AccordionContent>
                        <StepContent
                          step={step}
                          trialId={trialId}
                          apiBaseUrl={apiBaseUrl}
                        />
                      </AccordionContent>
                    </AccordionItem>
                  ))}
                </div>
              ))}
            </Accordion>
```

The `AccordionItem` block is byte-identical to the current one — only its wrapper changed. Radix's Accordion tracks items by context and refs, not by direct children, so nesting items in a `div` is safe; `expandedSteps` stays a flat list of `step-<idx>` values and the `#step-N` deep link keeps resolving.

- [ ] **Step 4: Verify the build**

```bash
cd frontend && pnpm run build && pnpm run lint
```

Expected: `✓ Compiled successfully`, no lint errors.

- [ ] **Step 5: Verify in the running app**

```bash
cd frontend && pnpm dev
```

Open a trial with a v4 summary on its Summary tab and confirm:
1. Group headers appear in step order with colored swatches and gist lines.
2. Rows show the agent's message preview, model, and `+`/`@` timing badges — not `Bash, Read`.
3. Expanding a row still renders full `StepContent`.
4. Typing in the search box hides non-matching steps and drops emptied groups; the count reads `N of M steps`.
5. Appending `#step-<id>` to the URL scrolls to and expands that step.
6. On a trial with **no** summary, the list renders flat exactly as before.

Report which of these you actually observed. A green build is not evidence for any of them.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/trajectory-viewer.tsx
git commit -m "feat(frontend): group trajectory steps under their taxonomy components"
```

---

### Task 4: Strip the Activity card back to visuals

**Files:**
- Modify: `frontend/src/components/trajectory-activity.tsx` (delete the steps-by-component section and `summarizeStep`)

**Interfaces:**
- Consumes: nothing new.
- Produces: nothing.

- [ ] **Step 1: Delete the duplicate list**

Remove the entire `{/* steps by component */}` block — the `<div className="space-y-4 pt-1">` wrapper and everything through its closing `</div>`, i.e. the whole `segments.map(...)` render. Keep the legend, the timeline, and the token heatmap.

- [ ] **Step 2: Delete `summarizeStep`**

Remove the `summarizeStep` function at the bottom of the file (its only caller was the deleted block) and its doc comment.

- [ ] **Step 3: Prune what the deletion orphaned**

`stepIdToIndex` and `onStepSelect` are still used — the timeline and heatmap bars call `select()`. Keep both props.

Check and remove anything now unused: `labelFor` is still used by the legend and the bar `title`s, so it stays. Let `pnpm run lint` decide the rest.

- [ ] **Step 4: Verify build, lint, and dead code**

```bash
cd frontend && pnpm run build && pnpm run lint && pnpm run dead-code
```

Expected: compiles clean, no unused-variable errors, and knip reports no newly-unused exports.

- [ ] **Step 5: Verify in the running app**

With `pnpm dev`, confirm the Activity card now shows only legend + timeline + token heatmap, that clicking a timeline bar still scrolls to and expands that step in the Trajectory card below, and that the star overlays still mark highlighted steps.

- [ ] **Step 6: Commit and open the PR**

```bash
git add frontend/src/components/trajectory-activity.tsx
git commit -m "refactor(frontend): drop the Activity card's duplicate step list"
git push -u origin feat/grouped-trajectory-steps
```

Open a PR against `main` describing the before/after and listing which manual checks were performed.

---

## Notes for the reviewer

- `groupStepsBySegment` is the only place a bug can hide silently. Its contract — first claim wins, unclaimed runs stay in place, interleaving splits into multiple runs — is exercised by Task 1's throwaway tests, which are not committed. If a test runner is ever added to this frontend, that function is the first thing to port over.
- Group ranges are computed from *visible* steps, so under an active search a header reads the span of its matches, not of the whole component. That is intentional.
