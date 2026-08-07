"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { ChevronDown } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { fetcher } from "@/lib/api";
import { encodeExperimentRouteParam } from "@/lib/utils";

type ProbeRow = {
  task_id: string;
  task_name: string;
  run_count: number;
  last_run_at: string | null;
  last_status: string;
  probe_names: string[];
};

type QaReportRow = {
  experiment_id: string;
  name: string;
  graded_experiment_id: string;
  graded_experiment_name: string | null;
  last_activity_at: string | null;
};

function QaReportsCard() {
  const { data } = useSWR<QaReportRow[]>("/api/qa-reports", fetcher);
  if (!data || data.length === 0) return null;

  return (
    <Card className="border-[#6f88b4]/20 shadow-xs">
      <CardHeader className="pb-3">
        <CardTitle className="text-base">QA reports</CardTitle>
        <p className="text-muted-foreground text-[11px]">
          Each report is an experiment that holds the QA and audit runs for
          the experiment it grades.
        </p>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Report</TableHead>
              <TableHead>Grades</TableHead>
              <TableHead className="w-[200px]">Last activity</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {data.map((row) => (
              <TableRow key={row.experiment_id}>
                <TableCell className="text-xs font-medium">
                  <Link
                    href={`/experiments/${encodeExperimentRouteParam(row.experiment_id)}`}
                    className="hover:underline"
                  >
                    {row.name}
                  </Link>
                </TableCell>
                <TableCell className="text-xs">
                  <Link
                    href={`/experiments/${encodeExperimentRouteParam(row.graded_experiment_id)}`}
                    className="text-muted-foreground hover:underline"
                  >
                    {row.graded_experiment_name ?? row.graded_experiment_id}
                  </Link>
                </TableCell>
                <TableCell className="text-muted-foreground text-[11px]">
                  {row.last_activity_at
                    ? new Date(row.last_activity_at).toLocaleString()
                    : "—"}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}

const PAGE_SIZE = 25;

function useDebouncedValue<T>(value: T, delayMs: number) {
  const [debouncedValue, setDebouncedValue] = useState(value);

  useEffect(() => {
    const timeoutId = window.setTimeout(
      () => setDebouncedValue(value),
      delayMs,
    );
    return () => window.clearTimeout(timeoutId);
  }, [delayMs, value]);

  return debouncedValue;
}

function ProbeNameFilter({
  options,
  selected,
  onChange,
}: {
  options: string[];
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  const toggle = (value: string) => {
    onChange(
      selected.includes(value)
        ? selected.filter((v) => v !== value)
        : [...selected, value],
    );
  };
  const label =
    selected.length === 0
      ? "All probes"
      : selected.length === 1
        ? selected[0]
        : `${selected.length} probes`;

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className="h-8 w-full justify-between border-[#6f88b4]/20 text-xs font-normal sm:w-[220px]"
        >
          <span className="truncate">{label}</span>
          <ChevronDown className="h-3.5 w-3.5 opacity-60" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="z-30 w-56 p-2">
        <div className="max-h-56 space-y-0.5 overflow-auto">
          {options.length === 0 ? (
            <p className="text-muted-foreground px-1 py-2 text-xs">
              No probe names
            </p>
          ) : (
            options.map((name) => (
              <label
                key={name}
                className="hover:bg-muted/60 flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-xs"
              >
                <Checkbox
                  checked={selected.includes(name)}
                  onCheckedChange={() => toggle(name)}
                />
                <span className="truncate">{name}</span>
              </label>
            ))
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}

export function QaRunsClient() {
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedProbes, setSelectedProbes] = useState<string[]>([]);
  const [page, setPage] = useState(0);
  const debouncedQuery = useDebouncedValue(
    searchQuery.trim().toLowerCase(),
    300,
  );
  const { data, error, isLoading } = useSWR<ProbeRow[]>("/api/probes", fetcher);

  useEffect(() => {
    setPage(0);
  }, [debouncedQuery, selectedProbes]);

  const probeOptions = useMemo(() => {
    const names = new Set<string>();
    for (const row of data ?? []) {
      for (const name of row.probe_names ?? []) names.add(name);
    }
    return Array.from(names).sort((a, b) => a.localeCompare(b));
  }, [data]);

  const filtered = useMemo(() => {
    const rows = data ?? [];
    return rows.filter((row) => {
      const matchesName =
        !debouncedQuery ||
        row.task_name.toLowerCase().includes(debouncedQuery);
      const matchesProbe =
        selectedProbes.length === 0 ||
        (row.probe_names ?? []).some((n) => selectedProbes.includes(n));
      return matchesName && matchesProbe;
    });
  }, [data, debouncedQuery, selectedProbes]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const currentPage = Math.min(page, pageCount - 1);
  const pageRows = filtered.slice(
    currentPage * PAGE_SIZE,
    currentPage * PAGE_SIZE + PAGE_SIZE,
  );

  return (
    <div className="space-y-6">
      <QaReportsCard />
      <Card className="border-[#6f88b4]/20 shadow-xs">
        <CardHeader className="flex flex-col gap-3 pb-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="space-y-1">
            <CardTitle className="text-base">Probe runs</CardTitle>
            <p className="text-muted-foreground text-[11px]">
              Tasks with probe runs, most recent first.
            </p>
          </div>
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
            <Input
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search tasks"
              className="h-8 w-full border-[#6f88b4]/20 sm:w-[220px]"
            />
            <ProbeNameFilter
              options={probeOptions}
              selected={selectedProbes}
              onChange={setSelectedProbes}
            />
          </div>
        </CardHeader>
        <CardContent>
          {error ? (
            <Alert variant="destructive">
              <AlertTitle>Failed to load probe runs</AlertTitle>
              <AlertDescription>
                {error instanceof Error ? error.message : String(error)}
              </AlertDescription>
            </Alert>
          ) : isLoading ? (
            <div className="text-muted-foreground py-8 text-center text-sm">
              Loading…
            </div>
          ) : !data || data.length === 0 ? (
            <div className="bg-card/60 text-muted-foreground rounded-lg border border-dashed border-[#6f88b4]/30 px-6 py-10 text-center text-sm">
              No probe runs yet. Start one from the &quot;Run Probe&quot; tab.
            </div>
          ) : filtered.length === 0 ? (
            <div className="bg-card/60 text-muted-foreground rounded-lg border border-dashed border-[#6f88b4]/30 px-6 py-10 text-center text-sm">
              No tasks match the current filters.
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Task</TableHead>
                  <TableHead className="w-[90px]">Runs</TableHead>
                  <TableHead className="w-[100px]">Last status</TableHead>
                  <TableHead className="w-[200px]">Last run</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {pageRows.map((row) => (
                  <TableRow key={row.task_id}>
                    <TableCell className="text-xs font-medium">
                      <Link
                        href={`/tasks/${row.task_id}/probe`}
                        className="hover:underline"
                      >
                        {row.task_name}
                      </Link>
                    </TableCell>
                    <TableCell className="text-muted-foreground text-xs">
                      {row.run_count}
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant="outline"
                        className="border-[#6f88b4]/30 px-1.5 py-0 text-[10px]"
                      >
                        {row.last_status}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-muted-foreground text-[11px]">
                      {row.last_run_at
                        ? new Date(row.last_run_at).toLocaleString()
                        : "—"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
          {filtered.length > PAGE_SIZE && (
            <div className="text-muted-foreground flex items-center justify-between pt-3 text-[11px]">
              <span>
                Showing {currentPage * PAGE_SIZE + 1}–
                {currentPage * PAGE_SIZE + pageRows.length} of {filtered.length}
              </span>
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-7 text-xs"
                  disabled={currentPage === 0}
                  onClick={() => setPage((p) => Math.max(0, p - 1))}
                >
                  Prev
                </Button>
                <span>
                  Page {currentPage + 1} of {pageCount}
                </span>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-7 text-xs"
                  disabled={currentPage >= pageCount - 1}
                  onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
                >
                  Next
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
