"use client";

import Link from "next/link";
import type { DeliveryTaskBoardRow } from "@/lib/types";
import { formatRelativeTime } from "@/lib/utils";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";

const STAGES = [
  ["pre_trial", "Pre-trial"],
  ["post_trial", "Post-trial"],
  ["verdict", "Verdict"],
] as const;
const LABELS: Record<string, string> = {
  completed: "Completed",
  failed: "Failed",
  error: "Failed",
  not_run: "Not run",
  unavailable: "Unavailable",
  accept: "Accept",
  reject: "Reject",
  pending: "Pending",
  queued: "Queued",
  running: "Running",
  paused: "Paused",
  retrying: "Retrying",
  cancelled: "Cancelled",
  blocked: "Blocked",
};

/** Each disclosure owns the explanation and evidence link for one review stage. */
export function DeliveryReviewsCell({
  row,
  taskHref,
  frozen,
}: {
  row: DeliveryTaskBoardRow;
  taskHref: string;
  frozen: boolean;
}) {
  return (
    <div className="space-y-1 text-xs">
      {STAGES.map(([key, label]) => {
        const stage = row.reviews?.[key];
        const status = stage
          ? (LABELS[stage.status] ?? stage.status)
          : "Not recorded";
        const href = stage?.trial_id
          ? `${taskHref}${taskHref.includes("?") ? "&" : "?"}trial=${encodeURIComponent(stage.trial_id)}`
          : `${taskHref}#${key === "pre_trial" ? "source-review" : key === "post_trial" ? "execution-review" : "verdict"}`;
        return (
          <Popover key={key}>
            <PopoverTrigger asChild>
              <button
                type="button"
                className="hover:bg-muted flex w-full items-baseline justify-between gap-3 rounded px-1 py-0.5 text-left"
                aria-label={`${label} for ${row.task_name}: ${status}${stage?.outdated ? ", outdated" : ""}`}
              >
                <span className="text-muted-foreground">{label}</span>
                <span
                  className={`text-right whitespace-normal ${
                    stage?.outdated
                      ? "text-amber-700 dark:text-amber-400"
                      : stage?.status === "failed" || stage?.status === "reject"
                        ? "text-red-700 dark:text-red-400"
                        : ""
                  }`}
                >
                  {status}
                  {stage?.outdated && " · outdated"}
                  {stage?.finished_at && (
                    <span className="text-muted-foreground">
                      {" "}
                      ·{" "}
                      {frozen
                        ? new Date(stage.finished_at).toLocaleDateString()
                        : formatRelativeTime(stage.finished_at)}
                    </span>
                  )}
                </span>
              </button>
            </PopoverTrigger>
            <PopoverContent
              align="end"
              className="w-96 max-w-[calc(100vw-2rem)] space-y-2 text-sm"
            >
              <p className="font-medium">
                {label} · {status}
                {stage?.outdated && " · outdated"} · v{row.version ?? "?"}
              </p>
              <p className="break-words whitespace-pre-wrap">
                {stage?.detail ??
                  "This delivery record does not contain separate review-stage details."}
              </p>
              {stage?.outdated && (
                <p>
                  This result does not establish a current review of this
                  version and its evidence.
                </p>
              )}
              {stage?.finished_at && (
                <p className="text-muted-foreground">
                  Finished {new Date(stage.finished_at).toLocaleString()}
                </p>
              )}
              {frozen && (
                <p className="text-muted-foreground">
                  Recorded at finalization. The linked task details are live.
                </p>
              )}
              <Link href={href} className="underline">
                Open {label.toLowerCase()} details
              </Link>
            </PopoverContent>
          </Popover>
        );
      })}
    </div>
  );
}
