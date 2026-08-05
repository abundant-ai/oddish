"use client";

import useSWR, { type SWRResponse } from "swr";
import type { Trial } from "@/lib/types";

/** Returns true while the trial's analysis is queued or running on the server. */
export function isAnalysisStatusActive(
  status: Trial["analysis_status"],
): boolean {
  return status === "pending" || status === "queued" || status === "running";
}

// If a fetch hangs forever, the components using this hook would show a
// loading state forever. This timeout makes a hung fetch fail like a
// normal error, and the components then fall back to the trial data they
// already have.
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
 * Fetches one trial by its id.
 *
 * Every component that calls this hook with the same id shares a single
 * request and a single copy of the data, instead of each component
 * fetching the trial on its own. While the trial's analysis is queued or
 * running, the hook refetches every 5 seconds so the result appears on
 * its own; the refetching stops once the analysis finishes. Passing null
 * as the id fetches nothing.
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
