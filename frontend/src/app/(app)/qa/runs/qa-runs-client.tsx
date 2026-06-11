"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import useSWR from "swr";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
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

type ProbeRow = {
  task_id: string;
  task_name: string;
  run_count: number;
  last_run_at: string | null;
  last_status: string;
};

type TaskOption = { id: string; name: string };

const PAGE_SIZE = 25;

function useDebouncedValue<T>(value: T, delayMs: number) {
  const [debouncedValue, setDebouncedValue] = useState(value);

  useEffect(() => {
    const timeoutId = window.setTimeout(
      () => setDebouncedValue(value),
      delayMs
    );
    return () => window.clearTimeout(timeoutId);
  }, [delayMs, value]);

  return debouncedValue;
}

function NewProbeRunDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const router = useRouter();
  const [query, setQuery] = useState("");
  const { data, error } = useSWR<TaskOption[]>(
    open ? "/api/tasks" : null,
    fetcher
  );

  const filtered = useMemo(() => {
    const list = data ?? [];
    const q = query.trim().toLowerCase();
    const matches = q
      ? list.filter((t) => t.name.toLowerCase().includes(q))
      : list;
    return matches.slice(0, 50);
  }, [data, query]);

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (!v) setQuery("");
        onOpenChange(v);
      }}
    >
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>New probe run</DialogTitle>
          <DialogDescription className="text-muted-foreground text-xs">
            Pick a task to probe. You&apos;ll land on its probe page to
            configure and launch the run.
          </DialogDescription>
        </DialogHeader>
        <Input
          autoFocus
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search tasks…"
          className="h-8 border-[#6f88b4]/20"
        />
        <div className="max-h-72 overflow-y-auto rounded-md border border-[#6f88b4]/20">
          {error ? (
            <div className="text-destructive p-3 text-xs">
              Failed to load tasks.
            </div>
          ) : !data ? (
            <div className="text-muted-foreground p-3 text-xs">Loading…</div>
          ) : filtered.length === 0 ? (
            <div className="text-muted-foreground p-3 text-xs">
              No matching tasks.
            </div>
          ) : (
            filtered.map((t) => (
              <button
                key={t.id}
                type="button"
                onClick={() => router.push(`/tasks/${t.id}/probe`)}
                className="hover:bg-muted focus-visible:bg-muted block w-full px-3 py-2 text-left text-xs focus-visible:outline-none"
              >
                {t.name}
              </button>
            ))
          )}
        </div>
        {filtered.length === 50 && (
          <p className="text-muted-foreground text-[10px]">
            Showing first 50 — type to filter.
          </p>
        )}
      </DialogContent>
    </Dialog>
  );
}

export function QaRunsClient() {
  const [pickerOpen, setPickerOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [page, setPage] = useState(0);
  const debouncedQuery = useDebouncedValue(
    searchQuery.trim().toLowerCase(),
    300
  );
  const { data, error, isLoading } = useSWR<ProbeRow[]>("/api/probes", fetcher);

  useEffect(() => {
    setPage(0);
  }, [debouncedQuery]);

  const filtered = useMemo(() => {
    const rows = data ?? [];
    if (!debouncedQuery) return rows;
    return rows.filter((row) =>
      row.task_name.toLowerCase().includes(debouncedQuery)
    );
  }, [data, debouncedQuery]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const currentPage = Math.min(page, pageCount - 1);
  const pageRows = filtered.slice(
    currentPage * PAGE_SIZE,
    currentPage * PAGE_SIZE + PAGE_SIZE
  );

  return (
    <div className="space-y-6">
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
            <Button
              type="button"
              size="sm"
              className="h-8 text-xs"
              onClick={() => setPickerOpen(true)}
            >
              + New probe run
            </Button>
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
              No probe runs yet. Start one with &quot;+ New probe run&quot;.
            </div>
          ) : filtered.length === 0 ? (
            <div className="bg-card/60 text-muted-foreground rounded-lg border border-dashed border-[#6f88b4]/30 px-6 py-10 text-center text-sm">
              No tasks match the current search.
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

      <NewProbeRunDialog open={pickerOpen} onOpenChange={setPickerOpen} />
    </div>
  );
}
