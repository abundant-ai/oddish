"use client";

import useSWR from "swr";
import type { AgentCapabilities } from "@/lib/types";

export function agentCapabilitiesKey(
  taskId: string,
  version: number,
  apiBaseUrl = "/api"
): string {
  return `${apiBaseUrl}/tasks/${encodeURIComponent(taskId)}/agent-capabilities?version=${version}`;
}

/**
 * Shared capability-analysis request. A 202 means the backend durably queued
 * generation, so keep the SWR value null and poll until the stored block is
 * returned. The task overview and Capabilities pane deliberately use the same
 * key: opening the pane reuses the overview's eager warmup.
 */
export function useAgentCapabilities(
  taskId: string | null,
  version: number | null,
  { apiBaseUrl = "/api", enabled = true } = {}
) {
  return useSWR<AgentCapabilities | null>(
    enabled && taskId && version !== null
      ? agentCapabilitiesKey(taskId, version, apiBaseUrl)
      : null,
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
