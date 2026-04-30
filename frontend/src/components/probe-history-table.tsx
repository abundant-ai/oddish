"use client";

import Link from "next/link";
import useSWR from "swr";
import { useAuth } from "@clerk/nextjs";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8800";

type Trial = {
  id: string;
  agent: string;
  status: string;
  started_at: string | null;
  reward: number | null;
  // harbor_config is not currently exposed on TrialResponse; the optional
  // chain below is defensive — once Task 15 surfaces this field the filter
  // will start narrowing the table to probe-only runs.
  harbor_config?: { mode?: string } | null;
};

function statusLabel(t: Trial): string {
  if (t.status === "queued" || t.status === "pending") return "queued";
  if (t.status === "running") return "running";
  if (t.status === "success") return "done";
  if (t.status === "failed") return "failed";
  return t.status;
}

function resultLabel(t: Trial): string {
  if (t.reward === null || t.reward === undefined) return "—";
  return t.reward >= 0.5
    ? `Cheat (reward=${t.reward.toFixed(2)})`
    : `Clean (reward=${t.reward.toFixed(2)})`;
}

export function ProbeHistoryTable({ taskId }: { taskId: string }) {
  const { getToken } = useAuth();

  const fetcher = async (url: string) => {
    let token: string | null = null;
    try {
      token = await getToken({ template: "oddish" });
    } catch {
      // Template missing — fall back to default session token.
    }
    if (!token) {
      token = await getToken();
    }
    const res = await fetch(url, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  };

  const { data, error } = useSWR<Trial[]>(
    `${API_URL}/tasks/${taskId}/trials`,
    fetcher,
    { refreshInterval: 5000 },
  );

  if (error)
    return (
      <p className="text-sm text-red-500">
        Failed to load history: {error.message}
      </p>
    );
  if (!data)
    return <p className="text-sm text-muted-foreground">Loading history…</p>;

  // If harbor_config is exposed (Task 15+), narrow to probe runs only.
  // Otherwise show every trial for the task — better than hiding the whole
  // history if the field hasn't been wired through yet.
  const anyHaveHarborConfig = data.some(
    (t) => t.harbor_config !== undefined && t.harbor_config !== null,
  );
  const probes = anyHaveHarborConfig
    ? data.filter((t) => t.harbor_config?.mode === "probe")
    : data;

  return (
    <div>
      <h2 className="mb-3 text-lg font-medium">History</h2>
      {probes.length === 0 ? (
        <p className="text-sm text-muted-foreground">No probe runs yet.</p>
      ) : (
        <table className="w-full text-sm">
          <thead className="text-left text-muted-foreground">
            <tr>
              <th className="py-2 pr-4 font-medium">Timestamp</th>
              <th className="py-2 pr-4 font-medium">Agent</th>
              <th className="py-2 pr-4 font-medium">Status</th>
              <th className="py-2 pr-4 font-medium">Result</th>
              <th className="py-2 font-medium"></th>
            </tr>
          </thead>
          <tbody>
            {probes.map((t) => (
              <tr key={t.id} className="border-t">
                <td className="py-2 pr-4 font-mono text-xs">
                  {t.started_at ? new Date(t.started_at).toLocaleString() : "—"}
                </td>
                <td className="py-2 pr-4">{t.agent}</td>
                <td className="py-2 pr-4">{statusLabel(t)}</td>
                <td className="py-2 pr-4">{resultLabel(t)}</td>
                <td className="py-2">
                  <Link
                    href={`/tasks/${taskId}/probe/${t.id}`}
                    className="text-xs underline"
                  >
                    View →
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
