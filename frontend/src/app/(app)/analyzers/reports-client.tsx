"use client";

import Link from "next/link";
import { useState } from "react";
import useSWR from "swr";
import { Check, Copy } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
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

const CLI_COMMAND = "oddish report create -e <experiment-id>";

function CopyCommand({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex items-center gap-2">
      <code className="bg-muted rounded px-2 py-1 font-mono text-xs">{command}</code>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="h-7 px-2"
        onClick={() => {
          navigator.clipboard?.writeText(command).then(
            () => {
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            },
            () => {},
          );
        }}
        aria-label="Copy command"
      >
        {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
      </Button>
    </div>
  );
}

export function ReportsClient() {
  const { data, error, isLoading } = useSWR<Report[]>("/api/reports", fetcher, {
    // Reports are created from the CLI, so this list has to surface rows the tab
    // never created. The app disables focus revalidation globally; re-enable it
    // here (run the command, tab back) and keep a slow poll for an idle tab.
    revalidateOnFocus: true,
    refreshInterval: (rows) =>
      (rows ?? []).some(
        (r) =>
          r.status === "pending" || r.status === "queued" || r.status === "running",
      )
        ? 5000
        : 30000,
  });
  const [query, setQuery] = useState("");

  const q = query.trim().toLowerCase();
  const filtered = (data ?? []).filter((r) => r.name.toLowerCase().includes(q));

  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="space-y-2 py-4 text-sm">
          <p className="text-muted-foreground">
            Reports analyze agent trajectories across one or more experiments. Create
            one from the CLI — you can omit a name and it&apos;s auto-generated as{" "}
            <code className="font-mono text-xs">report_&lt;N&gt;_&lt;experiment&gt;</code>.
          </p>
          <CopyCommand command={CLI_COMMAND} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between gap-4">
          <CardTitle>Reports</CardTitle>
          <Input
            placeholder="Search reports…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="max-w-xs"
          />
        </CardHeader>
        <CardContent>
          {error ? (
            <div className="text-sm text-red-500">Failed to load reports.</div>
          ) : isLoading ? (
            <div className="text-muted-foreground text-sm">Loading…</div>
          ) : filtered.length === 0 ? (
            <div className="text-muted-foreground text-sm">
              {data && data.length > 0
                ? "No reports match your search."
                : "No reports yet. Run the command above to create one."}
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
                {filtered.map((r) => (
                  <TableRow key={r.id}>
                    <TableCell className="text-sm font-medium">
                      <Link href={`/analyzers/${r.id}`} className="hover:underline">
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
    </div>
  );
}
