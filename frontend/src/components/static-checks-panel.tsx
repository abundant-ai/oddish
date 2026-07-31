import { cn } from "@/lib/utils";
import { Skeleton } from "@/components/ui/skeleton";
import { SeverityGroups } from "@/components/qa-report/action-items";
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
  className,
}: {
  findings?: PreTrialFinding[] | null;
  status?: string | null;
  error?: string | null;
  costUsd?: number | null;
  onRerun: () => void;
  rerunning: boolean;
  queueError?: string | null;
  className?: string;
}) {
  const items = findings ?? [];
  const state = staticCheckState(status, items.length);
  const running = state === "running";

  return (
    <div className={cn("flex flex-col gap-3 p-4", className)}>
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-muted-foreground font-mono text-[11px] font-semibold tracking-wider uppercase">
          Static checks
        </h2>
        <span className="text-muted-foreground font-mono text-[11px]">
          {staticCheckSummary(state, items.length)}
          {hasDisplayableCostUsd(costUsd)
            ? ` · ${formatCostUsd(costUsd)}`
            : ""}
        </span>
        <button
          type="button"
          disabled={rerunning || running}
          onClick={onRerun}
          className="text-muted-foreground hover:text-foreground border-border ml-auto rounded border px-2 py-0.5 font-mono text-[10px] font-medium disabled:cursor-not-allowed disabled:opacity-50"
          title="Audit this task's source with the latest prompt"
        >
          {rerunning
            ? "Queuing…"
            : state === "unaudited"
              ? "Run checks"
              : "Re-run checks"}
        </button>
      </div>

      {queueError ? (
        <p className="text-[11px] text-red-500">{queueError}</p>
      ) : null}

      {state === "unaudited" ? (
        <p className="text-muted-foreground text-sm leading-relaxed">
          No static checks have run for this task. The checks audit the task
          source — verifier completeness, oracle correctness, and information
          leaks — before any agent attempts it.
        </p>
      ) : running ? (
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
        <SeverityGroups items={items} />
      )}
    </div>
  );
}
