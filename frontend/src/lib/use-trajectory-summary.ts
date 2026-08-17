"use client";

import useSWR from "swr";
import type { TrajectorySummary } from "@/lib/types";

type PendingSummaryStatus = "queued" | "running" | "retrying" | "blocked";

export type TrajectorySummaryResource =
  | { status: "ready"; summary: TrajectorySummary }
  | {
      status: PendingSummaryStatus;
      summary: null;
      jobId?: string;
      retryAfterMs: number;
    };

const pendingStatuses = new Set<PendingSummaryStatus>([
  "queued",
  "running",
  "retrying",
  "blocked",
]);

export async function fetchTrajectorySummary(
  url: string
): Promise<TrajectorySummaryResource> {
  const response = await fetch(url, { credentials: "include" });
  const body = await response.json().catch(() => null);
  if (response.status === 202) {
    const rawStatus =
      body && typeof body === "object" && "status" in body
        ? String(body.status).toLowerCase()
        : "queued";
    const pendingStatus = pendingStatuses.has(rawStatus as PendingSummaryStatus)
      ? (rawStatus as PendingSummaryStatus)
      : "queued";
    return {
      status: pendingStatus,
      summary: null,
      jobId:
        body && typeof body === "object" && "job_id" in body
          ? String(body.job_id)
          : undefined,
      retryAfterMs:
        body &&
        typeof body === "object" &&
        "retry_after_ms" in body &&
        Number.isFinite(Number(body.retry_after_ms))
          ? Number(body.retry_after_ms)
          : 3000,
    };
  }
  if (!response.ok) {
    const error = new Error(
      body && typeof body === "object" && "detail" in body
        ? String(body.detail)
        : response.statusText || "Request failed"
    );
    (error as Error & { status?: number }).status = response.status;
    throw error;
  }
  return { status: "ready", summary: body as TrajectorySummary };
}

/**
 * The one SWR handle for a trial's trajectory summary. The viewer owns it and
 * passes the result to its summary, activity, and step-group children.
 */
export function useTrajectorySummary(
  trialId: string,
  apiBaseUrl = "/api",
  enabled = true
) {
  return useSWR<TrajectorySummaryResource>(
    enabled ? `${apiBaseUrl}/trials/${trialId}/trajectory/summary` : null,
    fetchTrajectorySummary,
    {
      revalidateOnFocus: false,
      refreshInterval: (value) =>
        value && value.status !== "ready" ? value.retryAfterMs : 0,
    }
  );
}
