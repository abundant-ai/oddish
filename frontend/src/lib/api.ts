export const fetcher = async <T>(url: string): Promise<T> => {
  const startedAt =
    typeof performance !== "undefined" ? performance.now() : 0;
  const res = await fetch(url, { credentials: "include" });
  let data: unknown = null;

  try {
    data = await res.json();
  } catch {
    data = null;
  }

  if (typeof window !== "undefined") {
    const elapsedMs = performance.now() - startedAt;
    const serverTiming = res.headers.get("server-timing");
    if (elapsedMs > 1000 || serverTiming) {
      const parts: string[] = [`total=${elapsedMs.toFixed(0)}ms`];
      if (serverTiming) {
        for (const entry of serverTiming.split(",")) {
          const [name, ...rest] = entry.trim().split(";");
          const dur = rest
            .map((s) => s.trim())
            .find((s) => s.startsWith("dur="));
          if (name && dur) {
            parts.push(`${name}=${dur.slice(4)}ms`);
          }
        }
      }
      // eslint-disable-next-line no-console
      console.log(`[fetch ${url.split("?")[0]}] ${parts.join(" ")}`);
    }
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
