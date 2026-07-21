# QA Analyzer — Plan D: Frontend (line anchors + deep-link + action-items panel)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render pre-trial + post-trial `ActionItem`s in the task dashboard, each with a clickable `file:line` link that opens the task file drawer scrolled to and highlighting the referenced lines, and visibly elevate items that were exploited.

**Architecture:** Add per-line `id="L{n}"` anchors + a highlight range to the Shiki file renderer (`CodeBlock`), drive the files drawer from URL state (`?file=verifier.py&line=42`) using the component's existing `window.history.replaceState` idiom + the `initialFilePath` prop, add TS types + grouping helpers mirroring `probe-summary.ts`, and build an `ActionItemsPanel` mirroring `probe-run-summary.tsx`.

**Tech Stack:** Next.js (App Router), React, TypeScript, SWR, Shiki, Tailwind. NOTE: the frontend has no test runner wired up (per CLAUDE.md), so verification is `pnpm tsc --noEmit` + `pnpm lint` + a manual check; pure helpers are written to be trivially unit-testable if vitest is later added.

## Global Constraints

- Depends on Plans A–C: the backend now returns `task.pre_trial = {items: ActionItem[]}` and per-trial `trial.analysis.action_items` / `trial.analysis.exploitation`. Field shapes are the source of truth (`oddish/src/oddish/analyze/models.py`).
- CORRECTION vs. spec: the file drawer renders code through **Shiki** — `FileRenderer` → `CodeRenderer` → `CodeBlock` (`code-block.tsx`, `dangerouslySetInnerHTML`). `code-highlight.tsx` (prism) is only used by `notebook-renderer.tsx`. Line anchors therefore go in the **Shiki `CodeBlock`** path, not `code-highlight.tsx`.
- URL state for drawer/file/line is pure client UI state → use the existing `window.history.replaceState` pattern already in `task-detail-client.tsx:97-114` (`readVersionFromQuery`/`writeVersionToQuery`), NOT a Next navigation, to avoid re-render/refetch.
- Types are hand-written TS mirrors of the Pydantic models (no codegen). Keep enums as string unions matching the backend values exactly.
- Deep-link drives the drawer via the `TaskFilesPanel` `initialFilePath` prop (`task-files-panel.tsx:110-115`) + a new line prop; scroll happens in the panel's `contentRef` container (`:1272`).
- Run all commands from `frontend/`: `pnpm tsc --noEmit`, `pnpm lint`, `pnpm dev` for manual verification.

## Interfaces produced (cross-task contract)

- `CodeBlock` gains `highlightLines?: [number, number] | null` and emits `id="L{n}"` per line.
- `TaskFilesPanel` gains `initialLine?: number | null` (+ existing `initialFilePath`), scrolls to `#L{n}` on load.
- `task-detail-client.tsx`: `readFileFromQuery()` / `writeFileToQuery(file, line)`; `openFileAtLine(file, line)` opens the drawer + sets URL.
- `frontend/src/lib/action-items.ts`: `ActionItem` type, `Dimension`/`ActionTier` unions, `TIER_META`, `DIMENSION_META`, `groupByDimension`, `sortByTier`.
- `Task.pre_trial_analysis?: { items: ActionItem[] } | null`; `Trial` gains `analysis?.action_items` / `analysis?.exploitation` typing.
- `ActionItemsPanel` component + mount in `task-detail-client.tsx`.

---

### Task 1: TS types + grouping helpers

**Files:**
- Create: `frontend/src/lib/action-items.ts`
- Modify: `frontend/src/lib/types.ts` (add `pre_trial_analysis` to `Task`; action-item fields on `Trial` analysis)
- Test (optional, only if a runner exists): `frontend/src/lib/action-items.test.ts`

- [ ] **Step 1: Write the helper + types**

```ts
// frontend/src/lib/action-items.ts
export type ActionItemSource = "pre_trial" | "post_trial";
export type ProblemType = "incompleteness" | "mismatch";
export type Dimension = "verifier" | "oracle" | "info_leakage";
export type ActionTier = "must_fix" | "should_fix" | "optional";

export interface ActionItem {
  id: string;
  source: ActionItemSource;
  problem_type: ProblemType;
  dimension: Dimension;
  file: string;
  line_start: number;
  line_end: number;
  title: string;
  detail: string;
  recommendation: string;
  tier: ActionTier;
  links_to?: string | null;
  exploited?: boolean;
  exploit_evidence?: string | null;
  causal?: boolean;
}

const TIER_ORDER: Record<ActionTier, number> = { must_fix: 0, should_fix: 1, optional: 2 };

export const TIER_META: Record<ActionTier, { label: string; cls: string }> = {
  must_fix: { label: "Must fix", cls: "bg-red-500/15 text-red-600" },
  should_fix: { label: "Should fix", cls: "bg-amber-500/15 text-amber-700" },
  optional: { label: "Optional", cls: "bg-slate-500/15 text-slate-600" },
};

export const DIMENSION_META: Record<Dimension, { label: string }> = {
  verifier: { label: "Verifier completeness" },
  oracle: { label: "Oracle correctness" },
  info_leakage: { label: "Info leakage" },
};

export function sortByTier(items: ActionItem[] | undefined): ActionItem[] {
  return [...(items ?? [])].sort(
    (a, b) => (TIER_ORDER[a.tier] ?? 9) - (TIER_ORDER[b.tier] ?? 9),
  );
}

export function groupByDimension(
  items: ActionItem[] | undefined,
): Record<Dimension, ActionItem[]> {
  const groups: Record<Dimension, ActionItem[]> = { verifier: [], oracle: [], info_leakage: [] };
  for (const item of items ?? []) (groups[item.dimension] ??= []).push(item);
  (Object.keys(groups) as Dimension[]).forEach((k) => (groups[k] = sortByTier(groups[k])));
  return groups;
}
```

In `frontend/src/lib/types.ts`: add to the `Task` interface (near `verdict` at `:216-218`):

```ts
  pre_trial_analysis?: { items: import("./action-items").ActionItem[] } | null;
```

and ensure the `Trial` type's `analysis` allows `action_items?: ActionItem[]` and `exploitation?: {...}[]` (extend the existing analysis typing, or type it as `Record<string, unknown>` if it is already loose — prefer explicit).

- [ ] **Step 2: (If a JS test runner exists) write + run a unit test**

```ts
// frontend/src/lib/action-items.test.ts
import { describe, it, expect } from "vitest";
import { sortByTier, groupByDimension } from "./action-items";

describe("action-items helpers", () => {
  it("sorts must_fix first", () => {
    const items = [
      { tier: "optional", dimension: "verifier" },
      { tier: "must_fix", dimension: "verifier" },
    ] as never[];
    expect(sortByTier(items)[0].tier).toBe("must_fix");
  });
  it("groups by dimension", () => {
    const g = groupByDimension([{ tier: "must_fix", dimension: "oracle" }] as never[]);
    expect(g.oracle.length).toBe(1);
    expect(g.verifier.length).toBe(0);
  });
});
```

Run: `cd frontend && pnpm vitest run src/lib/action-items.test.ts` — if `vitest` is not installed, SKIP this step (frontend has no runner) and rely on Step 3.

- [ ] **Step 3: Typecheck**

Run: `cd frontend && pnpm tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/action-items.ts frontend/src/lib/types.ts frontend/src/lib/action-items.test.ts
git commit -m "feat(frontend): action-item types + grouping helpers"
```

---

### Task 2: Per-line anchors + highlight range in the Shiki renderer

**Files:**
- Modify: `frontend/src/components/renderers/code-block.tsx` (Shiki transformer adds `id="L{n}"`; new `highlightLines` prop)
- Modify: `frontend/src/components/renderers/code-renderer.tsx` (pass `highlightLines` through)
- Modify: `frontend/src/components/renderers/file-renderer.tsx` (accept + forward `highlightLines`)

**Design:** Shiki's `codeToHtml` accepts a `transformers` array with a `line(node, line)` hook. Add a transformer that sets `node.properties.id = 'L' + line` on every line and adds a highlight class when `line` is within `highlightLines`. This yields anchorable `#L42` targets inside the existing `dangerouslySetInnerHTML` output.

- [ ] **Step 1: Add the transformer + prop**

In `code-block.tsx`, extend the props (currently `code`, `language`, `className`, `maxHeight`, `truncateAt`, `showCopyButton` at `:76-85`) with:

```ts
  highlightLines?: [number, number] | null;
```

At the Shiki call (`:121-127`), add a `transformers` option:

```ts
const highlight = highlightLines;
const html = await codeToHtml(code, {
  lang,
  themes: { /* keep existing themes */ },
  transformers: [
    {
      line(node, line) {
        node.properties = node.properties || {};
        node.properties.id = `L${line}`;
        if (highlight && line >= highlight[0] && line <= highlight[1]) {
          this.addClassToHast(node, "qa-line-highlight");
        }
      },
    },
  ],
});
```

Add a highlight style (in the component's wrapper `className` or a global CSS): `.qa-line-highlight { background: color-mix(in oklab, var(--paper-highlight, #fde68a) 35%, transparent); display: block; }`. Match the repo's existing token conventions (see `task-verdict-badge.tsx` for `--paper-*` variables).

Confirm the installed Shiki version supports `transformers` + `this.addClassToHast` (Shiki ≥1.0). If the project pins an older Shiki, instead post-process the HTML string to inject `id="L{n}"` per `<span class="line">` — a `code.split("\n").length`-driven regex wrap.

- [ ] **Step 2: Thread `highlightLines` through**

`code-renderer.tsx` (`:10-21`): add `highlightLines?: [number, number] | null` to props and pass to `<CodeBlock ... highlightLines={highlightLines} />`.
`file-renderer.tsx` (`:232-239`, the `"code"` case): add `highlightLines` to `FileRenderer` props and forward it to `<CodeRenderer ... />`.

- [ ] **Step 3: Typecheck + manual verify**

Run: `cd frontend && pnpm tsc --noEmit` → no errors.
Manual: `cd frontend && pnpm dev`, open a task, open the files drawer on a code file, inspect the DOM to confirm each line row has `id="L1"`, `id="L2"`, … and that a highlighted range shows the background.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/renderers/code-block.tsx \
        frontend/src/components/renderers/code-renderer.tsx \
        frontend/src/components/renderers/file-renderer.tsx
git commit -m "feat(frontend): per-line anchors + highlight range in code renderer"
```

---

### Task 3: URL-driven drawer open + scroll-to-line

**Files:**
- Modify: `frontend/src/components/task-files-panel.tsx` (add `initialLine` prop + scroll-to-`#L{n}` effect; pass `highlightLines` to `FileRenderer`)
- Modify: `frontend/src/app/(app)/tasks/[task_id]/task-detail-client.tsx` (`readFileFromQuery`/`writeFileToQuery`, `openFileAtLine`, thread `initialFilePath`/`initialLine`)

- [ ] **Step 1: Panel — accept line + scroll**

In `task-files-panel.tsx`: add `initialLine?: number | null` to `TaskFilesPanelProps` (`:81-122`) and destructure it (`:269-287`). Pass a highlight range into the mounted `FileRenderer` (`:1041-1047`): `highlightLines={initialLine ? [initialLine, initialLine] : null}`. Add a scroll effect after content renders, next to the existing scroll-reset (`:819-824`):

```tsx
useEffect(() => {
  if (!initialLine || !selectedFile) return;
  const t = setTimeout(() => {
    contentRef.current?.querySelector(`#L${initialLine}`)?.scrollIntoView({ block: "center" });
  }, 60); // allow Shiki html to mount
  return () => clearTimeout(t);
}, [initialLine, selectedFile, fileContent]);
```

(`fileContent` in the deps ensures it fires once the file's text has loaded.)

- [ ] **Step 2: Client — URL read/write + open helper**

In `task-detail-client.tsx`, next to `readVersionFromQuery`/`writeVersionToQuery` (`:97-114`) add:

```tsx
function readFileFromQuery(): { file: string | null; line: number | null } {
  if (typeof window === "undefined") return { file: null, line: null };
  const p = new URLSearchParams(window.location.search);
  const file = p.get("file");
  const lineRaw = p.get("line");
  return { file, line: lineRaw ? Number(lineRaw) : null };
}

function writeFileToQuery(file: string | null, line: number | null) {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  if (file) url.searchParams.set("file", file);
  else url.searchParams.delete("file");
  if (line != null) url.searchParams.set("line", String(line));
  else url.searchParams.delete("line");
  window.history.replaceState(window.history.state, "", url.toString());
}
```

Add drawer state for the target file/line and an opener:

```tsx
const [fileTarget, setFileTarget] = useState<{ file: string; line: number | null } | null>(null);

const openFileAtLine = useCallback((file: string, line: number | null) => {
  setFileTarget({ file, line });
  setDrawer({ mode: "task" /* match existing DrawerState shape */ });
  writeFileToQuery(file, line);
}, []);
```

On mount, hydrate from the URL (mirror the `version` mount read at `:718`):

```tsx
useEffect(() => {
  const { file, line } = readFileFromQuery();
  if (file) { setFileTarget({ file, line }); setDrawer({ mode: "task" }); }
}, []);
```

Thread the target into the `taskContent` `<TaskFilesPanel>` mount (`:1230`): add `initialFilePath={fileTarget?.file ?? undefined}` and `initialLine={fileTarget?.line ?? undefined}`. When the drawer closes, clear the URL: in the existing `onOpenChange`/`onClose` path (`:1211`, `:1231`), also call `writeFileToQuery(null, null)` and `setFileTarget(null)`.

- [ ] **Step 3: Typecheck + manual verify**

Run: `cd frontend && pnpm tsc --noEmit` → no errors.
Manual: `pnpm dev`; navigate to `…/tasks/<id>?file=verifier.py&line=12` → the files drawer opens, `verifier.py` is selected, and line 12 is scrolled into view + highlighted. Closing the drawer removes `file`/`line` from the URL.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/task-files-panel.tsx \
        frontend/src/app/\(app\)/tasks/\[task_id\]/task-detail-client.tsx
git commit -m "feat(frontend): URL-driven file drawer with scroll-to-line"
```

---

### Task 4: `ActionItemsPanel` + mount

**Files:**
- Create: `frontend/src/components/action-items-panel.tsx`
- Modify: `frontend/src/app/(app)/tasks/[task_id]/task-detail-client.tsx` (render the panel; pass `openFileAtLine`)

**Design:** Mirror `probe-run-summary.tsx` structure. Group by dimension, sort by tier within each; each item shows a tier badge, title, detail/recommendation, and a clickable `file:Lstart` link calling `openFileAtLine(item.file, item.line_start)`. Exploited items get a red "exploited" badge and elevated styling. Renders pre-trial items from `task.pre_trial_analysis.items` and post-trial items collected from `task.trials[].analysis.action_items`.

- [ ] **Step 1: Build the component**

```tsx
// frontend/src/components/action-items-panel.tsx
"use client";

import { ActionItem, DIMENSION_META, TIER_META, groupByDimension } from "@/lib/action-items";

interface ActionItemsPanelProps {
  items: ActionItem[];
  onOpenFile: (file: string, line: number | null) => void;
}

export function ActionItemsPanel({ items, onOpenFile }: ActionItemsPanelProps) {
  if (!items.length) {
    return (
      <div className="rounded border bg-muted/20 p-3 text-sm text-emerald-700">
        No QA action items — task held up.
      </div>
    );
  }
  const groups = groupByDimension(items);
  const dimensions = Object.keys(groups) as (keyof typeof groups)[];

  return (
    <div className="space-y-3 rounded border-2 border-primary/30 bg-primary/5 p-4">
      <p className="text-xs font-semibold uppercase tracking-wide text-foreground">QA action items</p>
      {dimensions.map((dim) =>
        groups[dim].length ? (
          <div key={dim} className="rounded border bg-muted/20 p-3">
            <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              {DIMENSION_META[dim].label}
            </p>
            <ul className="space-y-2">
              {groups[dim].map((item) => {
                const meta = TIER_META[item.tier] ?? TIER_META.should_fix;
                return (
                  <li key={item.id} className="flex items-start gap-2 text-sm">
                    <span className={`mt-0.5 shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${meta.cls}`}>
                      {meta.label}
                    </span>
                    <span className="leading-snug">
                      <span className="font-medium">{item.title}</span>
                      {item.exploited ? (
                        <span className="ml-2 rounded bg-red-500/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-red-600">
                          exploited
                        </span>
                      ) : null}
                      <span className="text-muted-foreground"> — {item.detail}</span>
                      <div className="mt-0.5 text-xs">
                        <button
                          type="button"
                          className="font-mono text-primary underline underline-offset-2"
                          onClick={() => onOpenFile(item.file, item.line_start)}
                        >
                          {item.file}:{item.line_start}
                          {item.line_end !== item.line_start ? `-${item.line_end}` : ""}
                        </button>
                        <span className="text-muted-foreground"> · {item.recommendation}</span>
                      </div>
                      {item.exploited && item.exploit_evidence ? (
                        <div className="mt-0.5 text-xs text-red-600/90">Exploited: {item.exploit_evidence}</div>
                      ) : null}
                    </span>
                  </li>
                );
              })}
            </ul>
          </div>
        ) : null,
      )}
    </div>
  );
}
```

- [ ] **Step 2: Collect items + mount in the task detail**

In `task-detail-client.tsx`, build the combined list (memoized):

```tsx
const actionItems = useMemo(() => {
  const pre = task?.pre_trial_analysis?.items ?? [];
  const post = (task?.trials ?? []).flatMap((t) => t.analysis?.action_items ?? []);
  return [...pre, ...post];
}, [task]);
```

Render near the verdict badge (find where `TaskVerdictBadge` is rendered and place `ActionItemsPanel` alongside):

```tsx
<ActionItemsPanel items={actionItems} onOpenFile={openFileAtLine} />
```

Import `ActionItemsPanel` at the top. `openFileAtLine` comes from Task 3.

- [ ] **Step 3: Typecheck + lint + manual verify**

Run: `cd frontend && pnpm tsc --noEmit && pnpm lint` → clean.
Manual: `pnpm dev`; on a task with pre-trial analysis, the panel lists items grouped by dimension; clicking `verifier.py:12` opens the drawer at that line; exploited items show the red badge + evidence.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/action-items-panel.tsx \
        frontend/src/app/\(app\)/tasks/\[task_id\]/task-detail-client.tsx
git commit -m "feat(frontend): action-items panel with file:line deep-links"
```

---

## Self-Review

**Spec coverage (Component 7):**
- Per-line `id` anchors + range highlight → Task 2 (Shiki path, corrected from the spec's `code-highlight.tsx`). ✓
- URL-driven viewer opening `?file=…&line=…` and scrolling → Task 3. ✓
- Action-items panel grouped by dimension/tier, clickable file:line deep-links, exploited elevation → Task 4. ✓
- Pre-trial + post-trial items surfaced → Task 4 (from `task.pre_trial_analysis` + `task.trials[].analysis.action_items`). ✓

**Placeholder scan:** Every step has concrete code. Two explicit "match the existing shape" instructions (the `DrawerState` literal in `openFileAtLine`, the highlight CSS token) require reading the neighbor code — flagged inline, not silent. The vitest step is explicitly optional because the frontend has no runner.

**Type consistency:** `ActionItem`/`Dimension`/`ActionTier` string unions match the backend enum values verbatim. `highlightLines: [number, number] | null` is consistent across `CodeBlock`/`CodeRenderer`/`FileRenderer`. `initialLine`/`initialFilePath` prop names consistent between `task-files-panel.tsx` and the mount in `task-detail-client.tsx`. `openFileAtLine(file, line)` signature matches the panel's `onOpenFile`.

**Deferred/known gap:** used `?line=` (single line) for the deep-link; the schema carries `line_start`+`line_end`, and Task 2 highlights the full range, but the URL only pins the start line for scroll — acceptable (range still highlights via the item's own render). Extend to `?lines=start-end` later if needed.
