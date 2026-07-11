"use client";

import Link from "next/link";
import useSWR from "swr";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { fetcher } from "@/lib/api";
import type { Report } from "@/lib/types";
import { NewReportDialog } from "@/components/new-report-dialog";

export function ReportsClient() {
  const { data, error, isLoading, mutate } = useSWR<Report[]>(
    "/api/reports",
    fetcher,
    {
      refreshInterval: (rows) =>
        (rows ?? []).some(
          (r) => r.status === "pending" || r.status === "queued" || r.status === "running",
        )
          ? 5000
          : 0,
    },
  );

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle>Reports</CardTitle>
        <NewReportDialog onCreated={() => mutate()} />
      </CardHeader>
      <CardContent>
        {error ? (
          <div className="text-sm text-red-500">Failed to load reports.</div>
        ) : isLoading ? (
          <div className="text-muted-foreground text-sm">Loading…</div>
        ) : !data || data.length === 0 ? (
          <div className="text-muted-foreground text-sm">
            No reports yet. Create one to analyze trajectories across experiments.
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Trials</TableHead>
                <TableHead>Bad</TableHead>
                <TableHead>Good</TableHead>
                <TableHead>Created</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.map((r) => (
                <TableRow key={r.id}>
                  <TableCell className="text-sm font-medium">
                    <Link href={`/reports/${r.id}`} className="hover:underline">
                      {r.name}
                    </Link>
                  </TableCell>
                  <TableCell>
                    <Badge variant="outline" className="text-[10px]">
                      {r.status}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-muted-foreground text-xs">
                    {r.num_trials ?? "—"}
                  </TableCell>
                  <TableCell className="text-muted-foreground text-xs">
                    {r.num_bad_failures ?? "—"}
                  </TableCell>
                  <TableCell className="text-muted-foreground text-xs">
                    {r.num_good_failures ?? "—"}
                  </TableCell>
                  <TableCell className="text-muted-foreground text-[11px]">
                    {r.created_at ? new Date(r.created_at).toLocaleString() : "—"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
