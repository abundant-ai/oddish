"use client";

import useSWR from "swr";
import type { AgentCapabilities } from "@/lib/types";

/**
 * The one SWR handle for a task version's agent-capabilities analysis.
 *
 * Hoisted out of `AgentCapabilitiesSection` because the public share pane has
 * to know whether an analysis exists *before* it decides to offer the pane at
 * all: the section is the only thing in that pane, so a 404 has to mean "no
 * entry in the sidebar" rather than "an entry that opens onto nothing". Both
 * callers share the key, so it stays one request.
 *
 * A 404 rejects with the number 404, which is the gate rather than a fault:
 * the version has too few classified runs, or nothing has been generated for
 * it yet. `shouldRetryOnError` is off so that answer is taken once.
 */
export function useAgentCapabilities(
  taskId: string | null,
  apiBaseUrl: string,
  version: number | null | undefined,
) {
  return useSWR<AgentCapabilities>(
    taskId && typeof version === "number"
      ? `${apiBaseUrl}/tasks/${encodeURIComponent(taskId)}/agent-capabilities` +
          `?version=${version}`
      : null,
    (url: string) =>
      fetch(url).then((r) => (r.ok ? r.json() : Promise.reject(r.status))),
    { shouldRetryOnError: false },
  );
}
