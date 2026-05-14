"use client";

import useSWR from "swr";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { fetcher } from "@/lib/api";
import type { BackfillEvent } from "@/lib/types";

interface Props {
  reportId: string;
}

export function ReportBackfillHistory({ reportId }: Props) {
  const { data: events } = useSWR<BackfillEvent[]>(
    `/api/reports/${reportId}/backfill/events`,
    fetcher,
    { refreshInterval: 30_000 },
  );

  if (!events || events.length === 0) return null;

  return (
    <Card>
      <CardHeader className="py-2">
        <div className="text-sm font-medium">Backfill history</div>
      </CardHeader>
      <CardContent className="overflow-x-auto p-0">
        <table className="min-w-full text-xs">
          <thead className="bg-muted/40 text-left">
            <tr>
              <th className="px-3 py-2">Event</th>
              <th className="px-3 py-2">When</th>
              <th className="px-3 py-2">By</th>
              <th className="px-3 py-2">Source</th>
              <th className="px-3 py-2">Trials</th>
              <th className="px-3 py-2">Est $</th>
              <th className="px-3 py-2">Status</th>
            </tr>
          </thead>
          <tbody>
            {events.map((ev) => (
              <tr key={ev.id} className="border-t">
                <td className="px-3 py-2 font-mono">{ev.id}</td>
                <td className="px-3 py-2">
                  {new Date(ev.initiated_at).toLocaleString()}
                </td>
                <td className="px-3 py-2">
                  {ev.initiated_by_display ?? "—"}
                </td>
                <td className="px-3 py-2">{ev.source}</td>
                <td className="px-3 py-2">{ev.trials_submitted}</td>
                <td className="px-3 py-2">
                  {ev.est_cost_usd_total !== null
                    ? `$${ev.est_cost_usd_total.toFixed(2)}`
                    : "—"}
                </td>
                <td className="px-3 py-2">
                  <StatusBadge status={ev.status} />
                  {ev.error_message ? (
                    <span
                      className="ml-2 text-red-300"
                      title={ev.error_message}
                    >
                      ⚠ {ev.error_message.slice(0, 60)}
                    </span>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}

function StatusBadge({ status }: { status: string }) {
  const cls =
    status === "submitted"
      ? "bg-green-500/10 text-green-300"
      : status === "partial"
        ? "bg-yellow-500/10 text-yellow-300"
        : "bg-red-500/10 text-red-300";
  return (
    <span className={`rounded px-1.5 py-0.5 text-[10px] uppercase ${cls}`}>
      {status}
    </span>
  );
}
