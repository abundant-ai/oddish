import type { QaRun } from "@/lib/types";

const ACTIVE_QA_RUN_STATUSES = new Set([
  "pending",
  "queued",
  "running",
  "retrying",
]);

export function qaRunsKey(
  apiBaseUrl: string,
  taskId: string | null,
  version: number | null | undefined
): string | null {
  if (!taskId || version === undefined) return null;
  const suffix = version === null ? "" : `?version=${version}`;
  return `${apiBaseUrl}/tasks/${taskId}/qa/runs${suffix}`;
}

export function qaRunsRefreshInterval(runs: QaRun[] | undefined): number {
  return (runs ?? []).some((run) => isQaRunActive(run.status)) ? 5000 : 0;
}

export function isQaRunActive(status: QaRun["status"]): boolean {
  return ACTIVE_QA_RUN_STATUSES.has(status);
}
