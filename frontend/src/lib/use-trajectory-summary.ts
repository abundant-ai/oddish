"use client";

import useSWR from "swr";
import { fetcher } from "@/lib/api";
import type { TrajectorySummary } from "@/lib/types";

interface PendingTrajectorySummary {
  status: "pending";
}

type TrajectorySummaryResponse =
  | TrajectorySummary
  | PendingTrajectorySummary
  | null;

function isPending(
  value: TrajectorySummaryResponse | undefined
): value is PendingTrajectorySummary {
  return !!value && "status" in value && value.status === "pending";
}

/**
 * The one owner for a trial's trajectory summary request. Pending summaries
 * poll quietly until the completion worker stores the derived JSONB value.
 */
export function useTrajectorySummary(
  trialId: string,
  apiBaseUrl = "/api",
  enabled = true
) {
  const request = useSWR<TrajectorySummaryResponse>(
    enabled ? `${apiBaseUrl}/trials/${trialId}/trajectory/summary` : null,
    fetcher,
    {
      revalidateOnFocus: false,
      refreshInterval: (latest) => (isPending(latest) ? 5000 : 0),
    }
  );
  const pending = isPending(request.data);
  const summary =
    request.data && !isPending(request.data) ? request.data : null;
  return {
    ...request,
    summary,
    isPending: pending,
  };
}
