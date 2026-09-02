"use client";

import useSWR from "swr";
import { fetcher } from "@/lib/api";
import type { ExperimentCostTotals } from "@/lib/types";

export type ExperimentCostTotalsResource =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; data: ExperimentCostTotals }
  | { status: "error"; message: string; isRetrying: boolean };

/** Owns the exact experiment cost rollup, refresh policy, and retry lifecycle. */
export function useExperimentCostTotals({
  url,
  hasActiveTrials,
}: {
  url: string | null;
  hasActiveTrials: boolean;
}) {
  const { data, error, isValidating, mutate } = useSWR<
    ExperimentCostTotals,
    Error
  >(url, fetcher, {
    refreshInterval: hasActiveTrials ? 30000 : 0,
    revalidateOnFocus: false,
  });
  // SWR can retain successful data while a later refresh fails. Keep that
  // exact rollup visible; the error state is only for a resource that has
  // never loaded successfully.
  const resource: ExperimentCostTotalsResource =
    url === null
      ? { status: "idle" }
      : data !== undefined
        ? { status: "ready", data }
        : error
          ? {
              status: "error",
              message: error.message,
              isRetrying: isValidating,
            }
          : { status: "loading" };

  return {
    resource,
    refresh: mutate,
  };
}
