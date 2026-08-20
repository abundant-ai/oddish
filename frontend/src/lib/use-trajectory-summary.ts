"use client";

import useSWR from "swr";
import useSWRMutation from "swr/mutation";
import type { TrajectorySummary } from "@/lib/types";

type PendingSummaryStatus = "queued" | "running" | "retrying" | "settling";

export type TrajectorySummaryResource =
  | { status: "ready"; summary: TrajectorySummary }
  | { status: "missing" }
  | {
      status: PendingSummaryStatus;
      summary: null;
      jobId: string;
      retryAfterMs: number;
    };

const pendingStatuses = new Set<PendingSummaryStatus>([
  "queued",
  "running",
  "retrying",
  "settling",
]);

export function parseTrajectorySummaryResponse(
  response: Pick<Response, "ok" | "status" | "statusText">,
  body: unknown
): TrajectorySummaryResource {
  if (response.status === 404) return { status: "missing" };
  if (response.status === 202) {
    if (!body || typeof body !== "object" || Array.isArray(body)) {
      throw new Error("Malformed trajectory summary pending response");
    }
    const status = "status" in body ? body.status : null;
    const jobId = "job_id" in body ? body.job_id : null;
    const retryAfterMs = "retry_after_ms" in body ? body.retry_after_ms : null;
    if (
      typeof status !== "string" ||
      !pendingStatuses.has(status as PendingSummaryStatus) ||
      typeof jobId !== "string" ||
      jobId.length === 0 ||
      typeof retryAfterMs !== "number" ||
      !Number.isFinite(retryAfterMs) ||
      retryAfterMs <= 0
    ) {
      throw new Error("Malformed trajectory summary pending response");
    }
    return {
      status: status as PendingSummaryStatus,
      summary: null,
      jobId,
      retryAfterMs,
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
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new Error("Malformed trajectory summary response");
  }
  return { status: "ready", summary: body as TrajectorySummary };
}

async function readTrajectorySummaryResponse(
  response: Response
): Promise<TrajectorySummaryResource> {
  const body = await response.json().catch(() => null);
  return parseTrajectorySummaryResponse(response, body);
}

export async function fetchTrajectorySummary(
  url: string
): Promise<TrajectorySummaryResource> {
  return readTrajectorySummaryResponse(
    await fetch(url, { credentials: "include" })
  );
}

interface UseTrajectorySummaryOptions {
  trialId: string;
  apiBaseUrl?: string;
  enabled?: boolean;
  canRegenerate?: boolean;
}

/** Owns the summary read, paid refresh event, cache transition, and polling. */
export function useTrajectorySummary({
  trialId,
  apiBaseUrl = "/api",
  enabled = true,
  canRegenerate = false,
}: UseTrajectorySummaryOptions) {
  const summaryUrl = enabled
    ? `${apiBaseUrl}/trials/${trialId}/trajectory/summary`
    : null;
  const summaryQuery = useSWR<TrajectorySummaryResource>(
    summaryUrl,
    fetchTrajectorySummary,
    {
      revalidateOnFocus: false,
      refreshInterval: (value) =>
        value && value.status !== "ready" && value.status !== "missing"
          ? value.retryAfterMs
          : 0,
    }
  );
  const refreshMutation = useSWRMutation<TrajectorySummaryResource>(
    canRegenerate ? summaryUrl : null,
    async (url: string) =>
      readTrajectorySummaryResponse(
        await fetch(url, { method: "POST", credentials: "include" })
      )
  );

  async function regenerate() {
    if (!summaryUrl || !canRegenerate) {
      throw new Error("Trajectory summary regeneration is not available");
    }
    const resource = await refreshMutation.trigger(undefined, {
      throwOnError: false,
    });
    if (!resource) return undefined;
    await summaryQuery.mutate(resource, { revalidate: false });
    return resource;
  }

  return {
    ...summaryQuery,
    regenerate,
    regenerationError: refreshMutation.error,
    isStartingRegeneration: refreshMutation.isMutating,
  };
}
