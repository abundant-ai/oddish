"use client";

import { useRouter } from "next/navigation";
import useSWR from "swr";
import {
  ArrowUpRight,
  Braces,
  FileWarning,
  Loader2,
  ScrollText,
} from "lucide-react";

import { fetcher } from "@/lib/api";
import {
  isQaRunActive,
  qaRunsKey,
  qaRunsRefreshInterval,
} from "@/lib/qa-runs-resource";
import { cn } from "@/lib/utils";
import type { QaRun, Trial } from "@/lib/types";
import { Skeleton } from "@/components/ui/skeleton";

function runLabel(run: QaRun): string {
  const model = run.model?.split("/").pop();
  return model ? `${run.agent} · ${model}` : run.agent;
}

function durationLabel(run: QaRun): string | null {
  if (!run.started_at || !run.finished_at) return null;
  const seconds = Math.max(
    0,
    Math.round(
      (Date.parse(run.finished_at) - Date.parse(run.started_at)) / 1000
    )
  );
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${seconds % 60}s`;
}

function statusClass(status: QaRun["status"]): string {
  if (status === "success") return "text-emerald-600 dark:text-emerald-400";
  if (status === "failed" || status === "skipped") return "text-red-500";
  return "text-blue-500";
}

export function QaRunsPanel({
  taskId,
  apiBaseUrl,
  version,
  onOpenTrial,
}: {
  taskId: string | null;
  apiBaseUrl: string;
  version?: number | null;
  onOpenTrial?: (trial: Trial) => boolean;
}) {
  const router = useRouter();
  const versionKnown = version !== undefined;
  const key = qaRunsKey(apiBaseUrl, taskId, version);
  const { data: runs, error } = useSWR<QaRun[]>(key, fetcher, {
    revalidateOnFocus: false,
    refreshInterval: qaRunsRefreshInterval,
  });

  const openTrace = (run: QaRun) => {
    if (onOpenTrial?.(run)) return;
    const params = new URLSearchParams({ trial: run.id, tab: "trajectory" });
    if (run.task_version_id) params.set("version", run.task_version_id);
    router.push(`/tasks/${run.task_id}?${params.toString()}`);
  };

  return (
    <div className="border-border flex flex-col gap-3 border-t p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-muted-foreground font-mono text-[11px] font-semibold tracking-wider uppercase">
          QA runs
        </h2>
        <span className="text-muted-foreground font-mono text-[11px]">
          {!versionKnown
            ? "Loading…"
            : error
              ? "Unavailable"
              : runs
                ? `${runs.length} run${runs.length === 1 ? "" : "s"}${
                    version != null ? ` · v${version}` : ""
                  }`
                : "Loading…"}
        </span>
      </div>

      {error ? (
        <p className="font-mono text-[11px] text-red-500">
          Unable to load QA run history.
        </p>
      ) : !runs ? (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-10 w-full rounded-lg" />
          <Skeleton className="h-10 w-full rounded-lg" />
        </div>
      ) : runs.length === 0 ? (
        <p className="text-muted-foreground text-sm">
          No QA execution has been recorded for this task version.
        </p>
      ) : (
        <div className="flex flex-col gap-1.5">
          {runs.map((run) => {
            const active = isQaRunActive(run.status);
            const duration = durationLabel(run);
            return (
              <div
                key={run.id}
                className="border-border bg-background/40 flex flex-col gap-2 rounded-lg border px-3 py-2"
              >
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  {active ? (
                    <Loader2
                      className="h-3.5 w-3.5 shrink-0 animate-spin text-blue-500"
                      aria-hidden="true"
                    />
                  ) : null}
                  <span
                    className={cn(
                      "font-mono text-[10px] font-semibold tracking-wider uppercase",
                      statusClass(run.status)
                    )}
                  >
                    {run.status}
                  </span>
                  <span className="text-muted-foreground min-w-0 flex-1 truncate text-[11px]">
                    {runLabel(run)}
                  </span>
                  {duration ? (
                    <span className="text-muted-foreground font-mono text-[10px]">
                      {duration}
                    </span>
                  ) : null}
                  {run.cost_usd != null ? (
                    <span className="text-muted-foreground font-mono text-[10px]">
                      ${run.cost_usd.toFixed(4)}
                    </span>
                  ) : null}
                </div>

                {run.error_message ? (
                  <p
                    className="line-clamp-2 font-mono text-[10px] text-red-500"
                    title={run.error_message}
                  >
                    {run.error_message}
                  </p>
                ) : null}

                <div className="flex flex-wrap gap-1.5">
                  <button
                    type="button"
                    onClick={() => openTrace(run)}
                    className="border-border text-muted-foreground hover:text-foreground inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px]"
                  >
                    Trace
                    <ArrowUpRight className="h-3 w-3" aria-hidden="true" />
                  </button>
                  {run.artifacts_available ? (
                    <>
                      <a
                        href={`${apiBaseUrl}/trials/${run.id}/analysis-output/artifact`}
                        target="_blank"
                        rel="noreferrer"
                        className="border-border text-muted-foreground hover:text-foreground inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px]"
                      >
                        <Braces className="h-3 w-3" aria-hidden="true" />
                        Generated JSON
                      </a>
                      {run.status === "failed" ? (
                        <a
                          href={`${apiBaseUrl}/trials/${run.id}/analysis-output/validation`}
                          target="_blank"
                          rel="noreferrer"
                          className="border-border text-muted-foreground hover:text-foreground inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px]"
                        >
                          <FileWarning className="h-3 w-3" aria-hidden="true" />
                          Validation errors
                        </a>
                      ) : null}
                      <a
                        href={`${apiBaseUrl}/trials/${run.id}/logs`}
                        target="_blank"
                        rel="noreferrer"
                        className="border-border text-muted-foreground hover:text-foreground inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px]"
                      >
                        <ScrollText className="h-3 w-3" aria-hidden="true" />
                        Logs
                      </a>
                    </>
                  ) : null}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
