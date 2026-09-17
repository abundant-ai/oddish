"use client";

import useSWR, { mutate, preload, type SWRResponse } from "swr";
import { fetcher } from "@/lib/api";
import { isActiveTrialStatus } from "@/lib/job-status";
import { markTrialForReload, trialRequestInit } from "@/lib/trial-fetch";
import type { Trial } from "@/lib/types";

/** Returns true while the trial's analysis is queued or running on the server. */
export function isAnalysisStatusActive(
  status: Trial["analysis_status"],
): boolean {
  return status === "pending" || status === "queued" || status === "running";
}

// No `cache: "no-store"` here: the backend decides per row whether the
// browser may keep the response (finished trials, a day) or must not (live
// ones). See `@/lib/trial-fetch` for the timeout and the reload marks.
async function trialFetcher(url: string): Promise<Trial> {
  return fetcher<Trial>(url, trialRequestInit(url));
}

/**
 * Builds the SWR cache key for one trial. The hook below reads through
 * this key, and every cache write that targets a trial must build its
 * key through this same function, so a write can never miss the entry
 * the hook reads.
 */
export function trialKey(apiBaseUrl: string, trialId: string): string {
  return `${apiBaseUrl}/trials/${encodeURIComponent(trialId)}`;
}

export function preloadTrial(apiBaseUrl: string, trialId: string) {
  return preload(trialKey(apiBaseUrl, trialId), trialFetcher);
}

/**
 * Refetches one trial from the server, replacing any copy the browser's
 * HTTP cache holds. Use after an action that changes a finished trial in
 * place (re-running its analysis, a task-level QA run settling onto it);
 * a plain SWR revalidate would read the cached copy back.
 */
export function refetchTrialFromServer(apiBaseUrl: string, trialId: string) {
  const key = trialKey(apiBaseUrl, trialId);
  markTrialForReload(key);
  return mutate<Trial>(key);
}

/**
 * Fetches one trial by its id.
 *
 * Every component that calls this hook with the same id shares a single
 * request and a single copy of the data, instead of each component
 * fetching the trial on its own. While the Harbor trial itself or its legacy
 * per-trial analysis is active, the hook refetches every 5 seconds so status,
 * cost, and results update in place. Returned data always came from this
 * endpoint; callers render lightweight rows separately. Passing null as the
 * id fetches nothing.
 */
export function useTrial(
  trialId: string | null | undefined,
  { apiBaseUrl = "/api" }: { apiBaseUrl?: string } = {},
): SWRResponse<Trial, Error> {
  return useSWR<Trial>(
    trialId ? trialKey(apiBaseUrl, trialId) : null,
    trialFetcher,
    {
      revalidateOnFocus: false,
      refreshInterval: (data) =>
        isActiveTrialStatus(data?.status) ||
        isAnalysisStatusActive(data?.analysis_status)
          ? 5000
          : 0,
    },
  );
}
