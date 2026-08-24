export const fetcher = async <T>(
  url: string,
  init?: RequestInit
): Promise<T> => {
  const res = await fetch(url, { credentials: "include", ...init });
  let data: unknown = null;

  try {
    data = await res.json();
  } catch {
    data = null;
  }

  if (!res.ok) {
    const errorData =
      typeof data === "object" && data
        ? (data as { detail?: unknown; error?: unknown })
        : null;
    const message =
      (typeof errorData?.detail === "string" && errorData.detail) ||
      (typeof errorData?.error === "string" && errorData.error) ||
      res.statusText ||
      "Request failed";
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
