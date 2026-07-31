import { cn } from "@/lib/utils";
import { Skeleton } from "@/components/ui/skeleton";
import { SeverityGroups } from "@/components/qa-report/action-items";
import { CopyJsonButton } from "@/components/qa-report/copy-json-button";
import { formatCostUsd, hasDisplayableCostUsd } from "@/lib/format";
import type { PreTrialFinding } from "@/lib/types";

export type StaticCheckState =
  | "unaudited"
  | "running"
  | "failed"
  | "clean"
  | "findings";

/**
 * What to say for a task's static checks. Empty findings mean three
 * different things depending on status: only `success` with no items is
 * genuinely "we looked and found nothing".
 */
export function staticCheckState(
  status: string | null | undefined,
  findingCount: number,
): StaticCheckState {
  if (!status) return "unaudited";
  const normalized = status.toLowerCase();
  if (normalized === "running" || normalized === "queued") return "running";
  if (normalized === "success") return findingCount > 0 ? "findings" : "clean";
  return "failed";
}

export function staticCheckSummary(
  state: StaticCheckState,
  findingCount: number,
): string {
  switch (state) {
    case "unaudited":
      return "Not run yet";
    case "running":
      return "Running…";
    case "failed":
      return "Failed";
    case "clean":
      return "Clean";
    case "findings":
      return `${findingCount} finding${findingCount === 1 ? "" : "s"}`;
  }
}

/**
 * The static checks view for a task: the source audit's state and its
 * findings, grouped by severity tier like the trial report card.
 */
export function StaticChecksPanel({
  findings,
  status,
  error,
  costUsd,
  onRerun,
  rerunning,
  queueError,
  loading,
  loadError,
  className,
}: {
  findings?: PreTrialFinding[] | null;
  status?: string | null;
  error?: string | null;
  costUsd?: number | null;
  onRerun: () => void;
  rerunning: boolean;
  queueError?: string | null;
  /** The checks state is still being fetched: an absent status must not
   * read as "unaudited" — a Run click on that misread wipes real findings. */
  loading?: boolean;
  loadError?: string | null;
  className?: string;
}) {
  const items = findings ?? [];
  const state = staticCheckState(status, items.length);
  const stateUnknown = Boolean(loading || loadError);
  // Only a live run blocks the button. A stale "queued" row must stay
  // re-queueable: re-queue is the backend's recovery path for queued jobs
  // that never got picked up.
  const auditRunning = (status ?? "").toLowerCase() === "running";

  return (
    <div className={cn("flex flex-col gap-3 p-4", className)}>
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-muted-foreground font-mono text-[11px] font-semibold tracking-wider uppercase">
          Static checks
        </h2>
        <span className="text-muted-foreground font-mono text-[11px]">
          {loading
            ? "Loading…"
            : loadError
              ? "Unavailable"
              : staticCheckSummary(state, items.length)}
          {!stateUnknown && hasDisplayableCostUsd(costUsd)
            ? ` · ${formatCostUsd(costUsd)}`
            : ""}
        </span>
        <div className="ml-auto flex items-center gap-2">
          {items.length > 0 ? (
            <CopyJsonButton value={items} label="the static check findings" />
          ) : null}
          <button
            type="button"
            disabled={rerunning || auditRunning || stateUnknown}
            onClick={onRerun}
            className="text-muted-foreground hover:text-foreground border-border rounded border px-2 py-0.5 font-mono text-[10px] font-medium disabled:cursor-not-allowed disabled:opacity-50"
            title="Runs on the task's current version"
          >
            {rerunning
              ? "Queuing…"
              : state === "unaudited"
                ? "Run checks"
                : "Re-run checks"}
          </button>
        </div>
      </div>

      {queueError ? (
        <p className="text-[11px] text-red-500">{queueError}</p>
      ) : null}

      {loading ? (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-8 w-full rounded-lg" />
          <Skeleton className="h-8 w-full rounded-lg" />
          <Skeleton className="h-3 w-2/5" />
        </div>
      ) : loadError ? (
        <p className="font-mono text-[11px] break-all text-red-500">
          {loadError}
        </p>
      ) : state === "unaudited" ? (
        // The header summary already says "Not run yet"; no body needed.
        null
      ) : state === "running" ? (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-8 w-full rounded-lg" />
          <Skeleton className="h-8 w-full rounded-lg" />
          <Skeleton className="h-3 w-2/5" />
        </div>
      ) : state === "failed" ? (
        <p className="font-mono text-[11px] break-all text-red-500">
          {error || "The static checks run failed."}
        </p>
      ) : state === "clean" ? (
        <p className="text-muted-foreground text-sm leading-relaxed">
          The checks found no defects in this task&apos;s source.
        </p>
      ) : (
        // The default tier copy narrates trial classification; these findings
        // come from the source audit, so the effect line speaks to the task.
        <SeverityGroups
          items={items}
          tierEffects={{
            must_fix:
              "The defect can decide trials — QA marks the task bad until it is fixed.",
            should_fix: "Does not change the verdict.",
            optional: "Does not change the verdict.",
          }}
        />
      )}
    </div>
  );
}
