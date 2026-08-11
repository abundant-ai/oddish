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

/** Step deep links landed in #724: the trial view resolves #step-<step_id>. */
function evidenceHref(trialId: string, stepIds: number[]): string {
  return `/trials/${encodeURIComponent(trialId)}#step-${stepIds[0]}`;
}

function stepRange(stepIds: number[]): string {
  const lo = Math.min(...stepIds);
  const hi = Math.max(...stepIds);
  return lo === hi ? `[${lo}]` : `[${lo}-${hi}]`;
}

function ObservationList({ items }: { items: BehaviorObservation[] }) {
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
              href={evidenceHref(ev.trial_id, ev.step_ids)}
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
  const { data } = useSWR<CohortComparison>(
    `${apiBaseUrl}/tasks/${encodeURIComponent(taskId)}/cohort-comparison`,
    (url: string) =>
      fetch(url).then((r) => (r.ok ? r.json() : Promise.reject(r.status))),
    { shouldRetryOnError: false },
  );

  // The gate is a 404 from the endpoint; render nothing rather than an empty box.
  if (!data || !data.categories.length) return null;

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
              <ObservationList items={cat.successful} />
            </div>
            <div className="flex flex-col gap-2">
              <span className="text-xs uppercase tracking-wide text-muted-foreground">
                Failing
              </span>
              <ObservationList items={cat.failing} />
            </div>
          </div>
        </div>
      ))}
    </section>
  );
}
