"use client";

import { useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import useSWR from "swr";
import { fetcher } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";

type Monitor = {
  id: string;
  name: string;
  model: string;
  credential_ref: string;
  alerts_enabled: boolean;
  last_outcome: "success" | "failure" | "monitor_error" | null;
  last_checked_at: string | null;
  last_success_at: string | null;
  error: string | null;
  incident_id: string | null;
  incident_opened_at: string | null;
  stale: boolean;
};
type Health = { enabled: boolean; timestamp: string; monitors: Monitor[] };
type Check = {
  id: string;
  checked_at: string;
  outcome: "success" | "failure" | "monitor_error";
  latency_ms: number;
  error: string | null;
  status_code: number | null;
  request_id: string | null;
};

export function EndpointHealthCard() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const selectedId = searchParams.get("endpoint");
  const [showAll, setShowAll] = useState(false);
  const [checkRequest, setCheckRequest] = useState<{
    id: string;
    observedAt: string | null;
    state: "sending" | "scheduled" | "error";
  } | null>(null);
  const { data, error, mutate } = useSWR<Health>(
    "/api/admin/endpoint-health",
    fetcher,
    { refreshInterval: 30000 }
  );
  const {
    data: history,
    error: historyError,
    mutate: retryHistory,
  } = useSWR<{ checks: Check[] }>(
    selectedId
      ? `/api/admin/endpoint-health/${encodeURIComponent(selectedId)}/checks`
      : null,
    fetcher,
    { refreshInterval: 30000 }
  );
  const monitors = data?.monitors ?? [];
  const issues = monitors.filter(
    (m) => m.stale || m.incident_id || m.last_outcome !== "success"
  );
  const visible = showAll ? monitors : issues;
  const selected = monitors.find((m) => m.id === selectedId);
  const lastCheck = monitors.reduce<string | null>(
    (oldest, m) =>
      !m.last_checked_at
        ? oldest
        : !oldest || m.last_checked_at < oldest
          ? m.last_checked_at
          : oldest,
    null
  );

  function selectMonitor(id: string | null) {
    const params = new URLSearchParams(searchParams.toString());
    if (id) params.set("endpoint", id);
    else params.delete("endpoint");
    router.push(`/admin?${params}`, { scroll: false });
  }

  async function requestCheck(id: string) {
    setCheckRequest({
      id,
      observedAt: monitors.find((m) => m.id === id)?.last_checked_at ?? null,
      state: "sending",
    });
    try {
      const response = await fetch(
        `/api/admin/endpoint-health/${encodeURIComponent(id)}/check`,
        { method: "POST" }
      );
      if (!response.ok) throw new Error("Could not schedule check");
      setCheckRequest({
        id,
        observedAt: monitors.find((m) => m.id === id)?.last_checked_at ?? null,
        state: "scheduled",
      });
    } catch {
      setCheckRequest({
        id,
        observedAt: monitors.find((m) => m.id === id)?.last_checked_at ?? null,
        state: "error",
      });
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Model endpoint health</CardTitle>
        <p className="text-muted-foreground text-sm">
          Connections that need attention. Checks run automatically.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {error ? (
          <Alert variant="destructive">
            <AlertTitle>Endpoint health unavailable</AlertTitle>
            <AlertDescription>
              Current checks could not be loaded.
              <Button variant="outline" size="sm" onClick={() => mutate()}>
                Retry
              </Button>
            </AlertDescription>
          </Alert>
        ) : !data ? (
          <p role="status">Loading endpoint health…</p>
        ) : !data.enabled ? (
          <p>Endpoint monitoring is disabled for this environment.</p>
        ) : monitors.length === 0 ? (
          <p>
            No connections are being monitored. An operator must configure
            monitoring before health can be reported.
          </p>
        ) : (
          <>
            {monitors.some((m) => m.last_checked_at && !m.alerts_enabled) && (
              <p className="text-muted-foreground text-sm">
                Slack alerts are disabled or no channel is configured. Results
                remain available here.
              </p>
            )}
            {issues.length === 0 && (
              <p role="status" className="text-sm">
                No endpoint issues detected. All connections checked since{" "}
                {lastCheck && new Date(lastCheck).toLocaleString()}.
              </p>
            )}
            {visible.map((m) => (
              <div key={m.id} className="space-y-2 rounded-md border p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{m.name}</span>
                  <Badge variant={m.incident_id ? "destructive" : "outline"}>
                    {m.stale
                      ? "Checks overdue"
                      : m.last_outcome === "monitor_error"
                        ? "Check could not run"
                        : m.incident_id
                          ? "Repeated failures"
                          : m.last_outcome === "failure"
                            ? "Confirming failure"
                            : "Healthy"}
                  </Badge>
                </div>
                <p className="text-muted-foreground text-xs break-all">
                  {m.model} · {m.credential_ref}
                </p>
                {m.error && <p className="text-sm">{m.error}</p>}
                <p className="text-muted-foreground text-xs">
                  Last check:{" "}
                  {m.last_checked_at
                    ? new Date(m.last_checked_at).toLocaleString()
                    : "No completed checks"}
                  {m.incident_opened_at &&
                    ` · Incident opened ${new Date(m.incident_opened_at).toLocaleString()}`}
                </p>
                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => selectMonitor(m.id)}
                  >
                    Inspect checks
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={
                      checkRequest?.id === m.id &&
                      checkRequest.state === "sending"
                    }
                    onClick={() => requestCheck(m.id)}
                  >
                    Check again
                  </Button>
                  {checkRequest?.id === m.id &&
                    checkRequest.state === "scheduled" &&
                    checkRequest.observedAt === m.last_checked_at && (
                      <span role="status" className="text-sm">
                        Check requested; waiting for the monitor.
                      </span>
                    )}
                  {checkRequest?.id === m.id &&
                    checkRequest.state === "error" && (
                      <span role="alert" className="text-destructive text-sm">
                        Could not request a check. Try again.
                      </span>
                    )}
                </div>
              </div>
            ))}
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setShowAll(!showAll)}
            >
              {showAll
                ? "Show issues only"
                : `All connections and history (${monitors.length})`}
            </Button>
          </>
        )}
        {selectedId && (
          <section
            aria-label="Endpoint check history"
            className="space-y-3 border-t pt-4"
          >
            <div className="flex items-center justify-between gap-2">
              <h3 className="font-medium">
                Recent checks{selected ? `: ${selected.name}` : ""}
              </h3>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => selectMonitor(null)}
              >
                Close history
              </Button>
            </div>
            <p className="text-muted-foreground text-xs">
              Latest 100 checks from the past 30 days. Times use your local
              timezone.
            </p>
            {historyError ? (
              <div role="alert">
                Could not load check history.{" "}
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => retryHistory()}
                >
                  Retry history
                </Button>
              </div>
            ) : !history ? (
              <p role="status">Loading checks…</p>
            ) : history.checks.length === 0 ? (
              <p>No recorded checks.</p>
            ) : (
              <ol className="max-h-96 space-y-3 overflow-y-auto text-sm">
                {history.checks.map((check) => (
                  <li key={check.id} className="space-y-1 border-b pb-2">
                    <p>
                      {new Date(check.checked_at).toLocaleString()} ·{" "}
                      {check.outcome === "success"
                        ? "Succeeded"
                        : check.outcome === "failure"
                          ? "Failed"
                          : "Monitor error"}{" "}
                      · {check.latency_ms} ms
                    </p>
                    {check.error && (
                      <p>
                        {check.error}
                        {check.status_code && ` (HTTP ${check.status_code})`}
                      </p>
                    )}
                    {check.request_id && (
                      <p className="text-muted-foreground text-xs break-all">
                        Provider request: {check.request_id}
                      </p>
                    )}
                  </li>
                ))}
              </ol>
            )}
          </section>
        )}
      </CardContent>
    </Card>
  );
}
