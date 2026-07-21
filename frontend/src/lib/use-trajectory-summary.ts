"use client";

import useSWR from "swr";
import { fetcher } from "@/lib/api";
import type { TrajectorySummary } from "@/lib/types";

/**
 * The one SWR handle for a trial's trajectory summary. Three components on the
 * Summary tab need it; a single key means a single request.
 */
export function useTrajectorySummary(trialId: string, apiBaseUrl = "/api") {
  return useSWR<TrajectorySummary | null>(
    `${apiBaseUrl}/trials/${trialId}/trajectory/summary`,
    fetcher,
    { revalidateOnFocus: false },
  );
}
