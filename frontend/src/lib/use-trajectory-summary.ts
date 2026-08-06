"use client";

import { useCallback, useEffect, useState } from "react";
import useSWR from "swr";
import { fetcher } from "@/lib/api";
import type { TrajectorySummary } from "@/lib/types";

interface SummaryStatus {
  status: "pending" | "failed";
  detail?: string;
}

type TrajectorySummaryResponse = TrajectorySummary | SummaryStatus | null;

const enqueueRequests = new Map<string, Promise<void>>();

async function enqueue(url: string): Promise<void> {
  const response = await fetch(url, { method: "POST", credentials: "include" });
  if (response.ok) return;
  throw Object.assign(
    new Error(response.statusText || "Failed to queue summary"),
    {
      status: response.status,
    }
  );
}

function enqueueOnce(url: string) {
  const request = enqueueRequests.get(url) ?? enqueue(url);
  enqueueRequests.set(url, request);
  request.catch(() => enqueueRequests.delete(url));
  return request;
}

function hasStatus(
  value: TrajectorySummaryResponse | undefined,
  status: SummaryStatus["status"]
): value is SummaryStatus {
  return !!value && "status" in value && value.status === status;
}

/**
 * The one owner for a trial's trajectory summary request. The first pending
 * read queues one durable job, then GET polls until the worker stores JSONB.
 */
export function useTrajectorySummary(
  trialId: string,
  apiBaseUrl = "/api",
  enabled = true
) {
  const url = enabled
    ? `${apiBaseUrl}/trials/${trialId}/trajectory/summary`
    : null;
  const [enqueueFailure, setEnqueueFailure] = useState<{
    url: string;
    error: Error & { status?: number };
  } | null>(null);
  const request = useSWR<TrajectorySummaryResponse>(url, fetcher, {
    revalidateOnFocus: false,
    refreshInterval: (latest) => (hasStatus(latest, "pending") ? 5000 : 0),
  });
  const { mutate } = request;
  const pending = hasStatus(request.data, "pending");
  const failed = hasStatus(request.data, "failed") ? request.data : null;

  useEffect(() => {
    if (!url) return;
    if (!pending) {
      if (request.data) enqueueRequests.delete(url);
      return;
    }
    let active = true;
    enqueueOnce(url)
      .then(() => mutate())
      .catch((error: Error & { status?: number }) => {
        if (active) setEnqueueFailure({ url, error });
      });
    return () => {
      active = false;
    };
  }, [mutate, pending, request.data, url]);

  const retry = useCallback(async () => {
    if (!url) return;
    setEnqueueFailure(null);
    enqueueRequests.delete(url);
    try {
      await enqueueOnce(url);
      await mutate();
    } catch (error) {
      setEnqueueFailure({ url, error: error as Error & { status?: number } });
    }
  }, [mutate, url]);

  let generationError =
    (enqueueFailure?.url === url ? enqueueFailure.error : undefined) ??
    request.error;
  if (!generationError && failed) {
    generationError = new Error(
      failed.detail || "Trajectory summary generation failed"
    ) as Error & { status?: number };
    generationError.status = 500;
  }
  const summary: TrajectorySummary | null =
    request.data && !("status" in request.data) ? request.data : null;
  if (summary) generationError = undefined;
  return {
    ...request,
    summary,
    isPending: pending && !generationError,
    error: generationError,
    retry,
  };
}
