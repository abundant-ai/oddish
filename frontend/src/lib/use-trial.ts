"use client";

import useSWR, { type SWRResponse } from "swr";
import type { Trial } from "@/lib/types";

/** True while a trial's QA analysis is queued or running server-side. */
export function isAnalysisStatusActive(
  status: Trial["analysis_status"],
): boolean {
  return status === "pending" || status === "queued" || status === "running";
}

// Bounds consumers' loading gates: a hung fetch must reject so they fall
// back to the slim grid row instead of pinning a skeleton forever.
const TRIAL_FETCH_TIMEOUT_MS = 15_000;

async function trialFetcher(url: string): Promise<Trial> {
  const res = await fetch(url, {
    credentials: "include",
    cache: "no-store",
    signal: AbortSignal.timeout(TRIAL_FETCH_TIMEOUT_MS),
  });
  let data: unknown = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (!res.ok) {
    const message =
      typeof data === "object" && data && "error" in data
        ? String((data as { error?: string }).error)
        : res.statusText || "Request failed";
    throw new Error(message);
  }
  return data as Trial;
}

/**
 * Subscribe to one trial by id through the shared SWR cache.
 *
 * The cache key is the trial URL, so every view of the same trial (the
 * drawer's full-detail upgrade, the analysis card) shares one request and
 * one copy of the data instead of issuing sibling fetches. While the
 * trial's analysis is queued or running the subscription polls; it stops
 * on terminal states. Pass a null id to subscribe to nothing (drawer
 * closed, public share view).
 */
export function useTrial(
  trialId: string | null | undefined,
  { apiBaseUrl = "/api" }: { apiBaseUrl?: string } = {},
): SWRResponse<Trial, Error> {
  return useSWR<Trial>(
    trialId ? `${apiBaseUrl}/trials/${encodeURIComponent(trialId)}` : null,
    trialFetcher,
    {
      revalidateOnFocus: false,
      refreshInterval: (data) =>
        isAnalysisStatusActive(data?.analysis_status) ? 5000 : 0,
    },
  );
}
