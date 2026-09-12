"use client";

import Link from "next/link";
import type { DeliveryTaskBoardRow } from "@/lib/types";
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

function reviewAge(iso: string): string {
  const minutes = Math.max(
    0,
    Math.floor((Date.now() - new Date(iso).getTime()) / 60000)
  );
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m ago`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)}h ago`;
  return `${Math.floor(minutes / 1440)}d ago`;
}

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
    <div className="grid grid-cols-[1fr_1fr_0.8fr] items-center gap-1 text-[11px]">
      {STAGES.map(([key, label]) => {
        const stage = row.reviews?.[key];
        const status = stage
          ? (LABELS[stage.status] ?? stage.status)
          : "Not recorded";
        const findings = row.defects.filter((finding) =>
          key === "pre_trial"
            ? finding.source === "pre_trial"
            : finding.source === "trial"
        ).length;
        const clear =
          stage?.status === "accept" ||
          (stage?.status === "completed" &&
            (key === "pre_trial"
              ? findings === 0
              : row.qa.status === "accepted" && findings === 0));
        const bad =
          stage?.status === "reject" ||
          (stage?.status === "completed" && findings > 0);
        const failed = stage?.status === "failed" || stage?.status === "error";
        const inactive =
          !stage || ["not_run", "unavailable"].includes(stage.status);
        const waiting =
          stage &&
          ["pending", "queued", "running", "paused", "retrying"].includes(
            stage.status
          );
        const tone = stage?.outdated
          ? "amber"
          : bad
            ? "red"
            : clear
              ? "green"
              : inactive
                ? "gray"
                : "amber";
        const symbol = stage?.outdated
          ? "!"
          : bad
            ? "x"
            : clear
              ? "✓"
              : inactive
                ? ""
                : waiting
                  ? "~"
                  : "?";
        const meaning = stage?.outdated
          ? `${status}, outdated`
          : bad && key !== "verdict"
            ? "Findings"
            : clear && key !== "verdict"
              ? "Clear"
              : failed
                ? "Could not complete"
                : status;
        const colors = {
          green:
            "border-emerald-600/25 bg-emerald-500/10 text-emerald-800 dark:text-emerald-300",
          red: "border-red-600/25 bg-red-500/10 text-red-800 dark:text-red-300",
          amber:
            "border-amber-600/25 bg-amber-500/10 text-amber-800 dark:text-amber-300",
          gray: "border-transparent text-muted-foreground",
        };
        const href = stage?.trial_id
          ? `${taskHref}${taskHref.includes("?") ? "&" : "?"}trial=${encodeURIComponent(stage.trial_id)}`
          : `${taskHref}#${key === "pre_trial" ? "source-review" : key === "post_trial" ? "execution-review" : "verdict"}`;
        return (
          <Popover key={key}>
            <PopoverTrigger asChild>
              <button
                type="button"
                className={`flex w-fit max-w-full min-w-0 flex-wrap items-center gap-x-1 gap-y-0 rounded border px-1.5 py-0.5 text-left whitespace-nowrap hover:brightness-110 focus-visible:outline-2 focus-visible:outline-offset-2 ${colors[tone]}`}
                aria-label={`${label} for ${row.task_name}: ${meaning}`}
              >
                {symbol && (
                  <span
                    aria-hidden="true"
                    className="w-2.5 shrink-0 text-center font-mono font-semibold"
                  >
                    {symbol}
                  </span>
                )}
                <span>
                  {key === "verdict" ? (inactive ? "—" : status) : label}
                  {key !== "verdict" &&
                    (inactive ||
                      waiting ||
                      failed ||
                      stage?.status === "blocked" ||
                      stage?.status === "cancelled") &&
                    `: ${stage ? (failed ? "error" : status.toLowerCase()) : "not recorded"}`}
                </span>
                {key === "verdict" && stage?.outdated && (
                  <span>· outdated</span>
                )}
                {key !== "verdict" && stage?.finished_at && (
                  <span className="text-muted-foreground text-[11px] tabular-nums">
                    (
                    {frozen
                      ? new Date(stage.finished_at).toLocaleDateString()
                      : reviewAge(stage.finished_at)}
                    )
                  </span>
                )}
              </button>
            </PopoverTrigger>
            <PopoverContent
              align="end"
              className="w-96 max-w-[calc(100vw-2rem)] space-y-2 text-sm"
            >
              <p className="font-medium">
                {label} · {meaning} · v{row.version ?? "?"}
              </p>
              <p className="break-words whitespace-pre-wrap">
                {stage?.detail ??
                  "This delivery record does not contain separate review-stage details."}
              </p>
              {stage?.status === "completed" &&
                !clear &&
                !bad &&
                !stage.outdated && (
                  <p>
                    Completion is recorded, but this row does not establish a
                    clear outcome.
                  </p>
                )}
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
