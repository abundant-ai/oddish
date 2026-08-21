"use client";

import { useEffect } from "react";
import useSWR from "swr";
import useSWRMutation from "swr/mutation";
import type { TrajectorySummary } from "@/lib/types";

type ActiveRefreshStatus = "queued" | "running" | "retrying" | "settling";

export type TrajectorySummaryResource = {
  summary: TrajectorySummary | null;
  refresh:
    | null
    | {
        status: ActiveRefreshStatus;
        jobId: string;
        retryAfterMs: number;
      }
    | {
        status: "failed";
        jobId: string;
        detail: string;
      };
};

const activeRefreshStatuses = new Set<ActiveRefreshStatus>([
  "queued",
  "running",
  "retrying",
  "settling",
]);

function parseRefreshPayload(
  refresh: object
): NonNullable<TrajectorySummaryResource["refresh"]> {
  const status = "status" in refresh ? refresh.status : null;
  const jobId = "job_id" in refresh ? refresh.job_id : null;
  if (typeof status !== "string" || typeof jobId !== "string" || !jobId) {
    throw new Error("Malformed trajectory summary refresh response");
  }
  if (status === "failed") {
    const detail = "detail" in refresh ? refresh.detail : null;
    if (typeof detail !== "string" || !detail) {
      throw new Error("Malformed trajectory summary refresh response");
    }
    return { status, jobId, detail };
  }
  const retryAfterMs =
    "retry_after_ms" in refresh ? refresh.retry_after_ms : null;
  if (
    !activeRefreshStatuses.has(status as ActiveRefreshStatus) ||
    typeof retryAfterMs !== "number" ||
    !Number.isFinite(retryAfterMs) ||
    retryAfterMs <= 0
  ) {
    throw new Error("Malformed trajectory summary refresh response");
  }
  return {
    status: status as ActiveRefreshStatus,
    jobId,
    retryAfterMs,
  };
}

export function parseTrajectorySummaryResponse(
  response: Pick<Response, "ok" | "status" | "statusText">,
  body: unknown
): TrajectorySummaryResource {
  if (response.status === 404) return { summary: null, refresh: null };
  if (
    body &&
    typeof body === "object" &&
    !Array.isArray(body) &&
    "refresh" in body
  ) {
    const summary = "summary" in body ? body.summary : undefined;
    const refresh = body.refresh;
    if (
      (summary !== null &&
        (typeof summary !== "object" || Array.isArray(summary))) ||
      (refresh !== null &&
        (typeof refresh !== "object" || Array.isArray(refresh)))
    ) {
      throw new Error("Malformed trajectory summary response");
    }
    if (refresh === null) {
      if (summary === undefined) {
        throw new Error("Malformed trajectory summary response");
      }
      return { summary: summary as TrajectorySummary | null, refresh: null };
    }
    return {
      summary: summary as TrajectorySummary | null,
      refresh: parseRefreshPayload(refresh),
    };
  }
  // During a rolling deploy, an older backend can still return the original
  // top-level HTTP 202 payload. It represents a refresh lifecycle, not a
  // published TrajectorySummary, and must keep the resource polling.
  if (response.status === 202 && body && typeof body === "object") {
    return { summary: null, refresh: parseRefreshPayload(body) };
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
  // Public share endpoints intentionally remain plain stored-column reads.
  return { summary: body as TrajectorySummary, refresh: null };
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
  initialResource?: TrajectorySummaryResource;
}

/** Owns the summary read, paid refresh event, cache transition, and polling. */
export function useTrajectorySummary({
  trialId,
  apiBaseUrl = "/api",
  enabled = true,
  canRegenerate = false,
  initialResource,
}: UseTrajectorySummaryOptions) {
  const summaryUrl = enabled
    ? `${apiBaseUrl}/trials/${trialId}/trajectory/summary`
    : null;
  const summaryQuery = useSWR<TrajectorySummaryResource>(
    summaryUrl,
    fetchTrajectorySummary,
    {
      revalidateOnFocus: false,
      fallbackData: initialResource,
      revalidateOnMount: initialResource === undefined,
    }
  );
  const activeRefresh =
    summaryQuery.data?.refresh?.status === "failed"
      ? null
      : summaryQuery.data?.refresh;
  const pollingJobId = activeRefresh?.jobId ?? null;
  const initialPollDelay = activeRefresh?.retryAfterMs ?? null;
  const revalidateSummary = summaryQuery.mutate;
  useEffect(() => {
    if (
      !summaryUrl ||
      !pollingJobId ||
      initialPollDelay === null ||
      summaryQuery.error
    ) {
      return;
    }

    let cancelled = false;
    let timer: number | undefined;
    function schedulePoll(afterMs: number) {
      timer = window.setTimeout(async () => {
        try {
          const next = await revalidateSummary();
          const nextRefresh =
            next?.refresh?.status === "failed" ? null : next?.refresh;
          if (cancelled || !nextRefresh || nextRefresh.jobId !== pollingJobId) {
            return;
          }
          // If SWR considers two responses equal, React will not rerender and
          // restart this effect. Continue the same lifecycle from the response.
          schedulePoll(nextRefresh.retryAfterMs);
        } catch {
          // SWR exposes the request error through summaryQuery.error. Stop this
          // lifecycle until the user explicitly retries the read.
        }
      }, afterMs);
    }

    schedulePoll(initialPollDelay);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [
    initialPollDelay,
    pollingJobId,
    revalidateSummary,
    summaryQuery.error,
    summaryUrl,
  ]);

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
      // The effect below owns every GET in this lifecycle. The mutation
      // default would revalidate the same SWR key before this POST response is
      // published, allowing an older response to be overwritten out of order.
      revalidate: false,
      throwOnError: false,
    });
    if (!resource) return undefined;
    // Publishing the POST response starts the effect-owned polling lifecycle.
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
