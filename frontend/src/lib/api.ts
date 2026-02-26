// Shared API utilities
export const API_URL = "/api";

// Shared SWR key for dashboard data - used by Nav health indicator and Dashboard page
export const DASHBOARD_SWR_KEY = "/api/dashboard";

export const fetcher = async <T>(url: string): Promise<T> => {
  const res = await fetch(url, { credentials: "include" });
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
    const err = new Error(message);
    (err as Error & { status?: number; info?: unknown }).status = res.status;
    (err as Error & { status?: number; info?: unknown }).info = data;
    throw err;
  }

  return data as T;
};

// Format Harbor stage for display
export function formatHarborStage(stage: string | null | undefined): string {
  if (!stage) return "Pending";

  const stageMap: Record<string, string> = {
    starting: "Initializing",
    trial_started: "Starting",
    environment_setup: "Environment Setup",
    agent_running: "Agent Running",
    verification: "Verification",
    completed: "Completed",
    cleanup: "Cleanup",
    cancelled: "Cancelled",
  };

  return (
    stageMap[stage] ||
    stage.replace(/_/g, " ").replace(/\b\w/g, (l) => l.toUpperCase())
  );
}
