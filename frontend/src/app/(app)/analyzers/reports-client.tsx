"use client";

import Link from "next/link";
import { useState } from "react";
import useSWR from "swr";
import { BarChart3, Check, Copy } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { fetcher } from "@/lib/api";
import { formatCostUsd } from "@/lib/format";
import type { Report } from "@/lib/types";

const CLI_COMMAND = "oddish report create -e <experiment-id>";

type AnalyzerCostItem = {
  analyzer_type: string;
  cost_usd: number;
  job_count: number;
  input_tokens: number;
  output_tokens: number;
};

type AnalyzerCosts = {
  total_cost_usd: number;
  total_job_count: number;
  total_input_tokens: number;
  total_output_tokens: number;
  by_type: AnalyzerCostItem[];
};

type PreTrialSetting = { enabled: boolean; can_manage: boolean };

const WINDOWS = [
  { value: "1", label: "Last 24 hours" },
  { value: "7", label: "Last 7 days" },
  { value: "30", label: "Last 30 days" },
  { value: "90", label: "Last 90 days" },
  { value: "0", label: "All time" },
];

function analyzerLabel(value: string) {
  return value
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function compact(value: number) {
  return new Intl.NumberFormat(undefined, { notation: "compact" }).format(
    value
  );
}

export function AnalyzerCostsPanel() {
  const [windowDays, setWindowDays] = useState("30");
  const [saving, setSaving] = useState(false);
  const { data, error, isLoading } = useSWR<AnalyzerCosts>(
    `/api/analyzers/costs?window_days=${windowDays}`,
    fetcher
  );
  const { data: setting, mutate: mutateSetting } = useSWR<PreTrialSetting>(
    "/api/settings/pre-trial-analysis",
    fetcher
  );

  async function setPreTrial(enabled: boolean) {
    if (!setting?.can_manage || saving) return;
    setSaving(true);
    try {
      const response = await fetch("/api/settings/pre-trial-analysis", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      if (!response.ok) throw new Error("Unable to update pre-trial analysis");
      await mutateSetting(await response.json(), { revalidate: false });
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between gap-4">
        <CardTitle className="flex items-center gap-2">
          <BarChart3 className="h-4 w-4" /> Analyzer spend
        </CardTitle>
        <Select value={windowDays} onValueChange={setWindowDays}>
          <SelectTrigger className="h-8 w-40 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {WINDOWS.map((window) => (
              <SelectItem key={window.value} value={window.value}>
                {window.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </CardHeader>
      <CardContent className="space-y-5">
        <div className="flex items-start justify-between gap-6 rounded-md border p-4">
          <div>
            <Label htmlFor="pre-trial-analysis" className="text-sm font-medium">
              Static checks
            </Label>
            <p className="text-muted-foreground mt-1 text-xs">
              Audit task source before trial classification for every analyzed
              task in this organization.
            </p>
          </div>
          <Checkbox
            id="pre-trial-analysis"
            checked={setting?.enabled ?? false}
            disabled={!setting?.can_manage || saving}
            onCheckedChange={(checked) => void setPreTrial(checked === true)}
            aria-label="Enable static checks for this organization"
          />
        </div>
        {error ? (
          <p className="text-destructive text-sm">
            Unable to load analyzer spend.
          </p>
        ) : isLoading ? (
          <p className="text-muted-foreground text-sm">Loading spend…</p>
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-3">
              <div className="rounded-md border p-3">
                <p className="text-muted-foreground text-xs">Total cost</p>
                <p className="mt-1 font-mono text-lg font-semibold">
                  {formatCostUsd(data?.total_cost_usd ?? 0)}
                </p>
              </div>
              <div className="rounded-md border p-3">
                <p className="text-muted-foreground text-xs">Analyzer calls</p>
                <p className="mt-1 font-mono text-lg font-semibold">
                  {compact(data?.total_job_count ?? 0)}
                </p>
              </div>
              <div className="rounded-md border p-3">
                <p className="text-muted-foreground text-xs">Tokens</p>
                <p className="mt-1 font-mono text-lg font-semibold">
                  {compact(
                    (data?.total_input_tokens ?? 0) +
                      (data?.total_output_tokens ?? 0)
                  )}
                </p>
              </div>
            </div>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Analyzer type</TableHead>
                  <TableHead className="text-right">Calls</TableHead>
                  <TableHead className="text-right">Input</TableHead>
                  <TableHead className="text-right">Output</TableHead>
                  <TableHead className="text-right">Cost</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {(data?.by_type ?? []).map((item) => (
                  <TableRow key={item.analyzer_type}>
                    <TableCell className="font-medium">
                      {analyzerLabel(item.analyzer_type)}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs">
                      {compact(item.job_count)}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs">
                      {compact(item.input_tokens)}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs">
                      {compact(item.output_tokens)}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs font-semibold">
                      {formatCostUsd(item.cost_usd)}
                    </TableCell>
                  </TableRow>
                ))}
                {!data?.by_type.length && (
                  <TableRow>
                    <TableCell
                      colSpan={5}
                      className="text-muted-foreground text-center"
                    >
                      No analyzer spend in this window.
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </>
        )}
      </CardContent>
    </Card>
  );
}

function CopyCommand({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex items-center gap-2">
      <code className="bg-muted rounded px-2 py-1 font-mono text-xs">
        {command}
      </code>
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
            () => {}
          );
        }}
        aria-label="Copy command"
      >
        {copied ? (
          <Check className="h-3.5 w-3.5" />
        ) : (
          <Copy className="h-3.5 w-3.5" />
        )}
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
          r.status === "pending" ||
          r.status === "queued" ||
          r.status === "running"
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
            Reports analyze agent trajectories across one or more experiments.
            Create one from the CLI — you can omit a name and it&apos;s
            auto-generated as{" "}
            <code className="font-mono text-xs">
              report_&lt;N&gt;_&lt;experiment&gt;
            </code>
            .
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
                      <Link
                        href={`/analyzers/${r.id}`}
                        className="hover:underline"
                      >
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
                      {r.created_at
                        ? new Date(r.created_at).toLocaleString()
                        : "—"}
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
