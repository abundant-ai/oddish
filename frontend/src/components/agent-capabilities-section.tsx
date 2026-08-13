"use client";

import useSWR from "swr";
import { componentLabel } from "@/lib/trajectory-segments";
import type {
  AgentCapabilities,
  BehaviorEvidence,
  BehaviorObservation,
} from "@/lib/types";

const CATEGORY_LABELS: Record<string, string> = {
  behavior_discovery: "Agent behavior discovery",
  planning: "Planning",
  testing_verification: "Testing and verification",
  debugging: "Debugging",
  scope_adherence: "Scope adherence",
  coherence: "Long-horizon coherence",
  environment_tooling: "Environment and tooling",
};

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
/** The step a citation points at: its own for step evidence, the first of the
 *  span for summary evidence — where the quoted behaviour starts. */
function anchorStep(ev: BehaviorEvidence): number | null {
  if (typeof ev.step_id === "number") return ev.step_id;
  const ids = ev.step_ids ?? [];
  return ids.length ? Math.min(...ids) : null;
}

function evidenceHref(
  taskId: string,
  trialId: string,
  step: number | null,
  taskVersionId?: string,
  shareToken?: string
): string {
  const params = new URLSearchParams();
  if (shareToken) params.set("task", taskId);
  else if (taskVersionId) params.set("version", taskVersionId);
  // Full id: #1203 reverted the shortened ?trial= form and deleted
  // shortTrialParam with it. The sync no longer rewrites this param, so
  // there is no rewrite for the fragment to survive either -- but
  // urlWithSearch still carries it, because every other drawer sync does
  // rebuild the address.
  params.set("trial", trialId);
  params.set("tab", "trajectory");
  if (shareToken) params.set("taskPane", "capabilities");
  const anchor = step === null ? "" : `#step-${step}`;
  const pathname = shareToken
    ? `/share/${encodeURIComponent(shareToken)}`
    : `/tasks/${encodeURIComponent(taskId)}`;
  return `${pathname}?${params.toString()}${anchor}`;
}

/** What the blue part of a citation reads as. Step evidence is one step of a
 *  run, so it says so; summary evidence keeps the component + span it always
 *  had. */
function citationLabel(ev: BehaviorEvidence): string {
  if (typeof ev.step_id === "number") return `step ${ev.step_id}`;
  const ids = ev.step_ids ?? [];
  return ids.length
    ? `${componentLabel(ev.trajectory_component ?? "")} ${stepRange(ids)}`
    : componentLabel(ev.trajectory_component ?? "");
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
  trialModels,
  linkEvidence,
  shareToken,
}: {
  items: BehaviorObservation[];
  taskId: string;
  taskVersionId?: string;
  trialModels?: Record<string, string>;
  linkEvidence: boolean;
  shareToken?: string;
}) {
  if (!items.length) {
    return (
      <p className="text-muted-foreground text-sm">No difference found.</p>
    );
  }
  return (
    <ul className="flex flex-col gap-3">
      {items.map((obs) => (
        <li
          key={`${obs.behavior_description}:${obs.evidence.map((ev) => ev.trial_id).join(",")}`}
          className="flex flex-col gap-1.5"
        >
          <span className="text-sm">{obs.behavior_description}</span>
          {obs.evidence.length > 0 ? (
            <details className="group">
              <summary className="text-muted-foreground hover:text-foreground w-fit cursor-pointer font-mono text-[11px]">
                {obs.evidence.length}{" "}
                {obs.evidence.length === 1 ? "example" : "examples"}
              </summary>
              <div className="border-border mt-2 flex flex-col gap-2 border-l pl-3">
                {obs.evidence.map((ev) => {
                  const body = (
                    <>
                      <span
                        className={
                          linkEvidence
                            ? "font-mono text-blue-600 dark:text-blue-400"
                            : "font-mono"
                        }
                      >
                        {citationLabel(ev)}
                      </span>{" "}
                      {trialModels?.[ev.trial_id] ? (
                        <span className="text-muted-foreground/80 font-mono">
                          {trialModels[ev.trial_id]}
                        </span>
                      ) : null}{" "}
                      — {ev.quote}
                    </>
                  );
                  const key = `${ev.trial_id}:${ev.step_id ?? (ev.step_ids ?? []).join("-")}`;
                  return linkEvidence ? (
                    <a
                      key={key}
                      href={evidenceHref(
                        taskId,
                        ev.trial_id,
                        anchorStep(ev),
                        taskVersionId,
                        shareToken
                      )}
                      className="text-muted-foreground text-xs underline-offset-4 hover:underline"
                    >
                      {body}
                    </a>
                  ) : (
                    <span key={key} className="text-muted-foreground text-xs">
                      {body}
                    </span>
                  );
                })}
              </div>
            </details>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

function categoryModels(
  items: BehaviorObservation[],
  trialModels?: Record<string, string>
): { model: string; trials: number }[] {
  const trialsByModel = new Map<string, Set<string>>();
  for (const observation of items) {
    for (const evidence of observation.evidence) {
      const model = trialModels?.[evidence.trial_id];
      if (!model) continue;
      const trials = trialsByModel.get(model) ?? new Set<string>();
      trials.add(evidence.trial_id);
      trialsByModel.set(model, trials);
    }
  }
  return Array.from(trialsByModel, ([model, trials]) => ({
    model,
    trials: trials.size,
  })).sort((a, b) => a.model.localeCompare(b.model));
}

function CategoryCohort({
  tone,
  items,
  taskId,
  taskVersionId,
  trialModels,
  linkEvidence,
  shareToken,
}: {
  tone: "successful" | "failing";
  items: BehaviorObservation[];
  taskId: string;
  taskVersionId?: string;
  trialModels?: Record<string, string>;
  linkEvidence: boolean;
  shareToken?: string;
}) {
  const models = categoryModels(items, trialModels);
  const successful = tone === "successful";
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-1.5">
        <span
          className={`${COHORT_HEADING} ${
            successful
              ? "text-emerald-600 dark:text-emerald-400"
              : "text-red-600 dark:text-red-400"
          }`}
        >
          {successful ? "Successful" : "Failed"}
        </span>
        {models.map((model) => (
          <span
            key={model.model}
            className="border-border text-muted-foreground rounded border px-1.5 py-0.5 font-mono text-[10px]"
            title={`${model.trials} cited ${model.trials === 1 ? "trial" : "trials"} from ${model.model}`}
          >
            {model.model} ×{model.trials}
          </span>
        ))}
      </div>
      <ObservationList
        items={items}
        taskId={taskId}
        taskVersionId={taskVersionId}
        trialModels={trialModels}
        linkEvidence={linkEvidence}
        shareToken={shareToken}
      />
    </div>
  );
}

export function AgentCapabilitiesSection({
  taskId,
  apiBaseUrl = "/api",
  version,
  linkEvidence = true,
  shareToken,
}: {
  taskId: string;
  apiBaseUrl?: string;
  /** Whether citations link to the cited step. False on a share page, where
      the task route is not reachable signed out. */
  linkEvidence?: boolean;
  /** Public share token used to keep evidence links inside the shared view. */
  shareToken?: string;
  /** Selected task version. Required, not optional: the comparison covers one
      version's cohorts, and an omitted param falls back server-side to the
      current version — which is the wrong answer beside an older version's
      trials, and has no right answer at all when the host is aggregating
      across versions. The host decides not to render instead. */
  version: number;
}) {
  const { data, error, isLoading } = useSWR<AgentCapabilities>(
    `${apiBaseUrl}/tasks/${encodeURIComponent(taskId)}/agent-capabilities?version=${version}`,
    (url: string) =>
      fetch(url).then((response) =>
        response.ok ? response.json() : Promise.reject(response.status)
      ),
    { shouldRetryOnError: false }
  );

  if (error === 404) {
    return (
      <section className="flex flex-col gap-2 p-4">
        <h3 className={SECTION_HEADING}>Capabilities</h3>
        <p className="text-muted-foreground text-xs">
          No capability analysis is available for this task version.
        </p>
      </section>
    );
  }

  if (isLoading) {
    return (
      <section className="flex flex-col gap-2 p-4">
        <h3 className={SECTION_HEADING}>Capabilities</h3>
        <p className="text-muted-foreground animate-pulse text-xs">
          Analyzing agent behavior across successful and failing runs
          <EllipsisDots />
        </p>
      </section>
    );
  }

  if (error) {
    return (
      <section className="flex flex-col gap-2 p-4">
        <h3 className={SECTION_HEADING}>Capabilities</h3>
        <p className="text-muted-foreground text-xs">
          Could not build the analysis
          {typeof error === "number" ? ` (${error})` : ""}. Reload to try again.
        </p>
      </section>
    );
  }

  if (!data) return null;

  if (!data.categories.length) {
    return (
      <section className="flex flex-col gap-2 p-4">
        <h3 className={SECTION_HEADING}>Capabilities</h3>
        <p className="text-muted-foreground text-xs">
          {/* One cohort has nothing to differ from, so "no differences held
              up" would describe work that was never attempted. */}
          {data.cohort_success.length > 0 && data.cohort_failure.length > 0
            ? `No differences held up against the stored trajectories for these ${data.cohort_success.length} successful and ${data.cohort_failure.length} failed runs.`
            : `Nothing held up against the stored trajectories for these ${
                data.cohort_success.length || data.cohort_failure.length
              } ${data.cohort_success.length > 0 ? "successful" : "failed"} runs.`}
        </p>
      </section>
    );
  }

  // Derived from the cohorts, not read from `mode` alone: comparisons stored
  // before that field existed still have to pick a layout.
  const showSuccessful = data.cohort_success.length > 0;
  const showFailing = data.cohort_failure.length > 0;
  const single = data.mode === "single" || !showSuccessful || !showFailing;

  return (
    <section className="flex flex-col gap-4 p-4">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h3 className={SECTION_HEADING}>Capabilities</h3>
        <span className="text-muted-foreground text-xs">
          {showSuccessful && showFailing
            ? `${data.cohort_success.length} successful, ${data.cohort_failure.length} failed trials`
            : showSuccessful
              ? `${data.cohort_success.length} successful trials \u00b7 no failures to compare against`
              : `${data.cohort_failure.length} failed trials \u00b7 no successes to compare against`}
        </span>
      </div>
      <div
        className={single ? "flex flex-col gap-2" : "grid gap-6 md:grid-cols-2"}
      >
        {showSuccessful ? (
          <div className="flex flex-wrap items-center gap-1.5">
            <span
              className={`${COHORT_HEADING} text-emerald-600 dark:text-emerald-400`}
            >
              Successful
            </span>
            {data.models?.successful?.map((model) => (
              <span
                key={model.model}
                className="border-border text-muted-foreground rounded border px-1.5 py-0.5 font-mono text-[10px]"
                title={`${model.trials} successful ${
                  model.trials === 1 ? "trial" : "trials"
                } from ${model.model}`}
              >
                {model.model} ×{model.trials}
              </span>
            ))}
          </div>
        ) : null}
        {showFailing ? (
          <div className="flex flex-wrap items-center gap-1.5">
            <span
              className={`${COHORT_HEADING} text-red-600 dark:text-red-400`}
            >
              Failed
            </span>
            {data.models?.failing?.map((model) => (
              <span
                key={model.model}
                className="border-border text-muted-foreground rounded border px-1.5 py-0.5 font-mono text-[10px]"
                title={`${model.trials} failed ${
                  model.trials === 1 ? "trial" : "trials"
                } from ${model.model}`}
              >
                {model.model} ×{model.trials}
              </span>
            ))}
          </div>
        ) : null}
      </div>
      {data.summary ? (
        <p className="text-foreground max-w-4xl text-sm leading-relaxed">
          {data.summary}
        </p>
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
      {data.categories.map((cat) => (
        <div
          key={`${cat.category}:${cat.label ?? ""}`}
          className="border-border flex flex-col gap-3 border-t pt-3"
        >
          <h4 className={CATEGORY_HEADING}>
            {CATEGORY_LABELS[cat.category] ?? cat.category}
            {cat.label ? `: ${discoveryLabel(cat.label)}` : ""}
          </h4>
          <div
            className={
              single ? "flex flex-col gap-2" : "grid gap-6 md:grid-cols-2"
            }
          >
            {showSuccessful ? (
              <CategoryCohort
                tone="successful"
                items={cat.successful}
                taskId={taskId}
                taskVersionId={data.task_version_id}
                trialModels={data.trial_models}
                linkEvidence={linkEvidence}
                shareToken={shareToken}
              />
            ) : null}
            {showFailing ? (
              <CategoryCohort
                tone="failing"
                items={cat.failing}
                taskId={taskId}
                taskVersionId={data.task_version_id}
                trialModels={data.trial_models}
                linkEvidence={linkEvidence}
                shareToken={shareToken}
              />
            ) : null}
          </div>
        </div>
      ))}
    </section>
  );
}
