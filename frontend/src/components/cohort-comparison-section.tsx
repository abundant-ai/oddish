"use client";

import useSWR from "swr";

import { componentLabel } from "@/lib/trajectory-segments";
import { shortTrialParam } from "@/lib/trial-url";
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

/** The panel's section-header type, shared with Findings and Trial QA
 *  (task-overview-panel.tsx:613, :654) so the overview reads as one column
 *  instead of three unrelated widgets. The category and cohort headings below
 *  reuse the same family and separate themselves by colour, not by size. */
const SECTION_HEADING =
  "text-foreground font-mono text-[13px] font-semibold tracking-wider uppercase";
const CATEGORY_HEADING =
  "text-foreground font-mono text-[11px] font-semibold tracking-wider uppercase";
const COHORT_HEADING =
  "font-mono text-[11px] font-semibold tracking-wider uppercase";

/** A trial opens in the task page's drawer via ?trial=<id> — there is no
 *  /trials/<id> route.
 *
 *  ?tab=trajectory lands on the tab the citation is quoting, and #step-<id>
 *  is the anchor TrajectoryViewer already resolves (trajectory-viewer.tsx:833
 *  matches /^#step-(\d+)$/ and scrolls to it), so the link arrives at the
 *  cited step rather than the top of the run. The first step of the span is
 *  the anchor: it is where the quoted behaviour starts.
 *
 *  ?version= carries the version id, not the number this endpoint takes: the
 *  page resolves ?trial= against the selected version's trials alone, so a
 *  citation from an older version's comparison would land on the current
 *  version with the drawer shut. Same reason the overview's own trial links
 *  carry it. */
function evidenceHref(
  taskId: string,
  trialId: string,
  stepIds: number[],
  taskVersionId?: string,
): string {
  const params = new URLSearchParams();
  if (taskVersionId) params.set("version", taskVersionId);
  // Short form, like every other trial link: the page's URL sync rewrites a
  // full id to the short one, and a rewrite is a replaceState the fragment
  // has to survive. Emitting what the sync would write means it has nothing
  // to correct.
  params.set("trial", shortTrialParam(trialId, taskId));
  params.set("tab", "trajectory");
  const anchor = stepIds.length ? `#step-${Math.min(...stepIds)}` : "";
  return `/tasks/${encodeURIComponent(taskId)}?${params.toString()}${anchor}`;
}

/** Discovery labels arrive from the model as identifiers (`subagent_delegation`).
 *  Render them as words; the prompt asks for prose but a stored label written
 *  before that rule still has to read properly. */
function discoveryLabel(label: string): string {
  return label.replace(/_/g, " ").trim();
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
              href={evidenceHref(
                taskId,
                ev.trial_id,
                ev.step_ids,
                taskVersionId,
              )}
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
        <h3 className={SECTION_HEADING}>Agent capability analysis</h3>
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
        <h3 className={SECTION_HEADING}>Agent capability analysis</h3>
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
        <h3 className={SECTION_HEADING}>Agent capability analysis</h3>
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
        <h3 className={SECTION_HEADING}>Agent capability analysis</h3>
        <span className="text-xs text-muted-foreground">
          {data.cohort_success.length} successful, {data.cohort_failure.length}{" "}
          failed trials
        </span>
      </div>
      {data.summary ? (
        <p className="text-sm text-foreground">{data.summary}</p>
      ) : null}
      {/* No thin-coverage warning. `thin_coverage` divides covered steps by
          the trial's FULL step count, but components are built from
          drop_inert_steps(trajectory) -- so an agent that pads its run with
          empty steps can never score above its non-padded fraction. Measured
          on scarf-cargotracker v1: all six flagged trials were gemini (0.10
          to 0.196, consistent with its 51-91% empty-step padding) while every
          Anthropic trial scored exactly 1.00. It flagged the agent, not the
          evidence. Restoring a warning here needs the summariser to persist
          its post-filter step count as the denominator. */}
      {data.categories.map((cat, i) => (
        <div
          key={i}
          className="border-border bg-background/40 flex flex-col gap-2 rounded-lg border p-3"
        >
          <h4 className={CATEGORY_HEADING}>
            {CATEGORY_LABELS[cat.category] ?? cat.category}
            {cat.label ? `: ${discoveryLabel(cat.label)}` : ""}
          </h4>
          <div className="grid gap-6 md:grid-cols-2">
            <div className="flex flex-col gap-2">
              <span
                className={`${COHORT_HEADING} text-emerald-600 dark:text-emerald-400`}
              >
                Successful
              </span>
              <ObservationList
                items={cat.successful}
                taskId={taskId}
                taskVersionId={data.task_version_id}
              />
            </div>
            <div className="flex flex-col gap-2">
              <span
                className={`${COHORT_HEADING} text-red-600 dark:text-red-400`}
              >
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
