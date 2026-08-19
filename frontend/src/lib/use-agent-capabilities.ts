"use client";

import useSWR from "swr";
import type { AgentCapabilities } from "@/lib/types";

/** Capability-analysis lifecycle. A 202 means generation was durably queued,
 * so poll until the stored analysis is available. */
export function useAgentCapabilities(
  taskId: string,
  version: number,
  { apiBaseUrl = "/api" } = {}
) {
  return useSWR<AgentCapabilities | null>(
    `${apiBaseUrl}/tasks/${encodeURIComponent(taskId)}/agent-capabilities?version=${version}`,
    async (url: string) => {
      const response = await fetch(url);
      if (response.status === 202) return null;
      if (!response.ok) throw response.status;
      return response.json();
    },
    {
      shouldRetryOnError: false,
      refreshInterval: (value) => {
        if (value === null) return 3000;
        if (value?.stale && value.regenerating) return 60_000;
        return 0;
      },
    }
  );
}
