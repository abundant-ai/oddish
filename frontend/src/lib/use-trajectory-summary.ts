"use client";

import useSWR from "swr";
import type { TrajectorySummary } from "@/lib/types";

/**
 * The one SWR handle for a trial's trajectory summary. Three components on the
 * Summary tab need it; a single key means a single request.
 */
export function useTrajectorySummary(
  trialId: string,
  apiBaseUrl = "/api",
  enabled = true,
) {
  const publicShare = apiBaseUrl.includes("/api/public/experiments/");
  return useSWR<TrajectorySummary | null>(
    enabled ? `${apiBaseUrl}/trials/${trialId}/trajectory/summary` : null,
    async (url: string) => {
      const response = await fetch(url, { credentials: "include" });
      if (publicShare && response.status === 202) return null;
      const body = await response.json().catch(() => null);
      if (!response.ok) {
        const error = new Error(
          body && typeof body === "object" && "detail" in body
            ? String(body.detail)
            : response.statusText || "Request failed",
        );
        (error as Error & { status?: number }).status = response.status;
        throw error;
      }
      return body as TrajectorySummary;
    },
    {
      revalidateOnFocus: false,
      refreshInterval: (value) => (publicShare && value === null ? 3000 : 0),
    },
  );
}
