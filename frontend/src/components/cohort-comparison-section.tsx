"use client";

import useSWR from "swr";

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
 *  emitted rather than linking somewhere that does not exist. */
function evidenceHref(taskId: string, trialId: string): string {
  const params = new URLSearchParams({ trial: trialId });
  return `/tasks/${encodeURIComponent(taskId)}?${params.toString()}`;
}

function stepRange(stepIds: number[]): string {
  const lo = Math.min(...stepIds);
  const hi = Math.max(...stepIds);
  return lo === hi ? `[${lo}]` : `[${lo}-${hi}]`;
}

function ObservationList({
  items,
  taskId,
}: {
  items: BehaviorObservation[];
  taskId: string;
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
              href={evidenceHref(taskId, ev.trial_id)}
              className="text-xs text-muted-foreground hover:underline"
            >
              <span className="font-mono">
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

export function CohortComparisonSection({
  taskId,
  apiBaseUrl = "/api",
}: {
  taskId: string;
  apiBaseUrl?: string;
}) {
  const { data, error, isLoading } = useSWR<CohortComparison>(
    `${apiBaseUrl}/tasks/${encodeURIComponent(taskId)}/cohort-comparison`,
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
      <section className="flex flex-col gap-2">
        <h3 className="text-sm font-semibold">Successful vs failing agents</h3>
        <p className="text-muted-foreground animate-pulse text-xs">
          Comparing successful and failing runs. The first view generates this,
          which takes a moment.
        </p>
      </section>
    );
  }

  if (error) {
    return (
      <section className="flex flex-col gap-2">
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
      <section className="flex flex-col gap-2">
        <h3 className="text-sm font-semibold">Successful vs failing agents</h3>
        <p className="text-muted-foreground text-xs">
          No differences held up against the stored trajectories for these{" "}
          {data.cohort_success.length} successful and {data.cohort_failure.length}{" "}
          failing runs.
        </p>
      </section>
    );
  }

  return (
    <section className="flex flex-col gap-4">
      <div className="flex items-baseline gap-3">
        <h3 className="text-sm font-semibold">Successful vs failing agents</h3>
        <span className="text-xs text-muted-foreground">
          {data.cohort_success.length} successful, {data.cohort_failure.length} failing
        </span>
      </div>
      {data.thin_coverage?.length ? (
        <p className="text-xs text-muted-foreground">
          {data.thin_coverage.length} trial
          {data.thin_coverage.length === 1 ? "" : "s"} in this comparison have
          summaries covering under half their run; evidence from them is thin.
        </p>
      ) : null}
      {data.categories.map((cat, i) => (
        <div key={i} className="flex flex-col gap-2 border-t pt-3">
          <h4 className="text-sm font-medium">
            {CATEGORY_LABELS[cat.category] ?? cat.category}
            {cat.label ? `: ${cat.label}` : ""}
          </h4>
          <div className="grid gap-6 md:grid-cols-2">
            <div className="flex flex-col gap-2">
              <span className="text-xs uppercase tracking-wide text-muted-foreground">
                Successful
              </span>
              <ObservationList items={cat.successful} taskId={taskId} />
            </div>
            <div className="flex flex-col gap-2">
              <span className="text-xs uppercase tracking-wide text-muted-foreground">
                Failing
              </span>
              <ObservationList items={cat.failing} taskId={taskId} />
            </div>
          </div>
        </div>
      ))}
    </section>
  );
}
