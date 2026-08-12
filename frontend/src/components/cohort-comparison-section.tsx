"use client";

import useSWR from "swr";

import { pooledDeltas, THIN_N } from "@/lib/cohort-metrics";
import { componentLabel } from "@/lib/trajectory-segments";
import type { BehaviorObservation, CohortComparison } from "@/lib/types";

const CATEGORY_LABELS: Record<string, string> = {
  behavior_discovery: "Agent behavior discovery",
  planning: "Planning",
  testing_verification: "Testing and verification",
  debugging: "Debugging",
  scope_adherence: "Scope adherence",
  coherence: "Long-horizon coherence",
  environment_tooling: "Environment and tooling",
};

/** A trial opens in the task page's drawer via ?trial=<id> — there is no
 *  /trials/<id> route, and the drawer resolves no step anchor, so none is
 *  emitted rather than linking somewhere that does not exist.
 *
 *  ?version= carries the version id, not the number this endpoint takes: the
 *  page resolves ?trial= against the selected version's trials alone, so a
 *  citation from an older version's comparison would land on the current
 *  version with the drawer shut. Same reason the overview's own trial links
 *  carry it. */
function evidenceHref(
  taskId: string,
  trialId: string,
  taskVersionId?: string,
): string {
  const params = new URLSearchParams();
  if (taskVersionId) params.set("version", taskVersionId);
  params.set("trial", trialId);
  return `/tasks/${encodeURIComponent(taskId)}?${params.toString()}`;
}

function stepRange(stepIds: number[]): string {
  const lo = Math.min(...stepIds);
  const hi = Math.max(...stepIds);
  return lo === hi ? `[${lo}]` : `[${lo}-${hi}]`;
}

/** A "." → ".." → "..." cycle for the generating copy. All three dots occupy
 *  their slot from the start and only their opacity cycles, so the sentence
 *  ahead of them never reflows. Hidden from assistive tech: the sentence
 *  already says the work is in flight. */
function EllipsisDots() {
  return (
    <span aria-hidden="true">
      .<span className="ellipsis-dot-2">.</span>
      <span className="ellipsis-dot-3">.</span>
    </span>
  );
}

function ObservationList({
  items,
  taskId,
  taskVersionId,
}: {
  items: BehaviorObservation[];
  taskId: string;
  taskVersionId?: string;
}) {
  if (!items.length) {
    return <p className="text-sm text-muted-foreground">No difference found.</p>;
  }
  return (
    <ul className="flex flex-col gap-3">
      {items.map((obs, i) => (
        <li key={i} className="flex flex-col gap-1.5">
          <span className="text-sm">{obs.behavior_description}</span>
          {obs.evidence.map((ev, j) => (
            <a
              key={j}
              href={evidenceHref(taskId, ev.trial_id, taskVersionId)}
              className="text-xs text-muted-foreground underline-offset-4 hover:underline"
            >
              {/* Only the component + step range carries the link colour. The
                  quote is the agent's own words, and colouring it too turns a
                  paragraph of body text blue. */}
              <span className="font-mono text-blue-600 dark:text-blue-400">
                {componentLabel(ev.trajectory_component)} {stepRange(ev.step_ids)}
              </span>{" "}
              — {ev.quote}
            </a>
          ))}
        </li>
      ))}
    </ul>
  );
}

/** Half the track, in percent, so a delta of ±1 fills one side exactly. */
function barGeometry(delta: number): { left: string; width: string } {
  const half = Math.min(Math.abs(delta), 1) * 50;
  return delta >= 0
    ? { left: "50%", width: `${half}%` }
    : { left: `${50 - half}%`, width: `${half}%` };
}

function CohortDeltaBars({ comparison }: { comparison: CohortComparison }) {
  const rows = pooledDeltas(comparison);
  if (rows.every((r) => r.n === 0)) return null;
  return (
    <div className="flex flex-col gap-1.5">
      <h4 className="text-sm font-semibold">Where runs diverged</h4>
      {rows.map((row) => (
        <div key={row.category} className="flex items-center gap-2 text-xs">
          <span className="text-muted-foreground w-40 shrink-0 truncate">
            {CATEGORY_LABELS[row.category] ?? row.category}
          </span>
          <div className="bg-background/40 relative h-3 flex-1 rounded-sm">
            {/* The centre rule is the cohort's own success share, not 0.5:
                the delta is measured against it. */}
            <span className="bg-border absolute inset-y-0 left-1/2 w-px" />
            {row.delta !== null && (
              <span
                className="absolute inset-y-0 rounded-sm"
                style={{
                  ...barGeometry(row.delta),
                  background: row.delta >= 0 ? "#059669" : "#dc2626",
                  // Thin evidence must not read as a confident bar.
                  opacity: row.n < THIN_N ? 0.45 : 1,
                }}
              />
            )}
          </div>
          <span className="text-muted-foreground w-20 shrink-0 text-right font-mono">
            {row.delta === null
              ? "—"
              : `${row.delta >= 0 ? "+" : ""}${row.delta.toFixed(2)}`}
            <span className="opacity-60"> n={row.n}</span>
          </span>
        </div>
      ))}
    </div>
  );
}

export function CohortComparisonSection({
  taskId,
  apiBaseUrl = "/api",
  version,
}: {
  taskId: string;
  apiBaseUrl?: string;
  /** Selected task version. Required, not optional: the comparison covers one
      version's cohorts, and an omitted param falls back server-side to the
      current version — which is the wrong answer beside an older version's
      trials, and has no right answer at all when the host is aggregating
      across versions. The host decides not to render instead. */
  version: number;
}) {
  const { data, error, isLoading } = useSWR<CohortComparison>(
    `${apiBaseUrl}/tasks/${encodeURIComponent(taskId)}/cohort-comparison` +
      `?version=${version}`,
    (url: string) =>
      fetch(url).then((r) => (r.ok ? r.json() : Promise.reject(r.status))),
    { shouldRetryOnError: false },
  );

  // A 404 is the gate, not a fault: the task has too few classified trials.
  // Render nothing for it. Everything else gets a visible state, because a
  // silent null makes "still generating", "generation failed" and "nothing to
  // show" indistinguishable — the first view triggers a model call that takes
  // real time, so an empty panel otherwise reads as broken.
  if (error === 404) return null;

  if (isLoading) {
    return (
      <section className="border-border flex flex-col gap-2 border-b p-4">
        <h3 className="text-sm font-semibold">Successful vs failing agents</h3>
        <p className="text-muted-foreground animate-pulse text-xs">
          Analyzing agent behavior across successful and failing runs
          <EllipsisDots />
        </p>
      </section>
    );
  }

  if (error) {
    return (
      <section className="border-border flex flex-col gap-2 border-b p-4">
        <h3 className="text-sm font-semibold">Successful vs failing agents</h3>
        <p className="text-muted-foreground text-xs">
          Could not build the comparison{typeof error === "number" ? ` (${error})` : ""}.
          Reload to try again.
        </p>
      </section>
    );
  }

  if (!data) return null;

  if (!data.categories.length) {
    return (
      <section className="border-border flex flex-col gap-2 border-b p-4">
        <h3 className="text-sm font-semibold">Successful vs failing agents</h3>
        <p className="text-muted-foreground text-xs">
          No differences held up against the stored trajectories for these{" "}
          {data.cohort_success.length} successful and {data.cohort_failure.length}{" "}
          failed runs.
        </p>
      </section>
    );
  }

  return (
    <section className="border-border flex flex-col gap-4 border-b p-4">
      <div className="flex items-baseline gap-3">
        <h3 className="text-sm font-semibold">Successful vs failing agents</h3>
        <span className="text-xs text-muted-foreground">
          {data.cohort_success.length} successful, {data.cohort_failure.length} failed
        </span>
      </div>
      <CohortDeltaBars comparison={data} />
      {data.thin_coverage?.length ? (
        <p className="text-xs text-muted-foreground">
          {data.thin_coverage.length} trial
          {data.thin_coverage.length === 1 ? "" : "s"} in this comparison have
          summaries covering under half their run; evidence from them is thin.
        </p>
      ) : null}
      {data.categories.map((cat, i) => (
        <div
          key={i}
          className="border-border bg-background/40 flex flex-col gap-2 rounded-lg border p-3"
        >
          <h4 className="text-sm font-medium">
            {CATEGORY_LABELS[cat.category] ?? cat.category}
            {cat.label ? `: ${cat.label}` : ""}
          </h4>
          <div className="grid gap-6 md:grid-cols-2">
            <div className="flex flex-col gap-2">
              <span className="text-xs uppercase tracking-wide text-emerald-600 dark:text-emerald-400">
                Successful
              </span>
              <ObservationList
                items={cat.successful}
                taskId={taskId}
                taskVersionId={data.task_version_id}
              />
            </div>
            <div className="flex flex-col gap-2">
              <span className="text-xs uppercase tracking-wide text-red-600 dark:text-red-400">
                Failed
              </span>
              <ObservationList
                items={cat.failing}
                taskId={taskId}
                taskVersionId={data.task_version_id}
              />
            </div>
          </div>
        </div>
      ))}
    </section>
  );
}
