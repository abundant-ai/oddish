"use client";

import { useMemo, useState } from "react";
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
    fetcher,
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
          <DialogDescription className="text-xs text-muted-foreground">
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
            <div className="p-3 text-xs text-destructive">
              Failed to load tasks.
            </div>
          ) : !data ? (
            <div className="p-3 text-xs text-muted-foreground">Loading…</div>
          ) : filtered.length === 0 ? (
            <div className="p-3 text-xs text-muted-foreground">
              No matching tasks.
            </div>
          ) : (
            filtered.map((t) => (
              <button
                key={t.id}
                type="button"
                onClick={() => router.push(`/tasks/${t.id}/probe`)}
                className="block w-full px-3 py-2 text-left text-xs hover:bg-muted focus-visible:bg-muted focus-visible:outline-none"
              >
                {t.name}
              </button>
            ))
          )}
        </div>
        {filtered.length === 50 && (
          <p className="text-[10px] text-muted-foreground">
            Showing first 50 — type to filter.
          </p>
        )}
      </DialogContent>
    </Dialog>
  );
}

export function QaRunsClient() {
  const [pickerOpen, setPickerOpen] = useState(false);
  const { data, error, isLoading } = useSWR<ProbeRow[]>("/api/probes", fetcher);

  return (
    <div className="space-y-6">
      <Card className="border-[#6f88b4]/20 shadow-xs">
        <CardHeader className="flex flex-col gap-3 pb-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="space-y-1">
            <CardTitle className="text-base">Probe runs</CardTitle>
            <p className="text-[11px] text-muted-foreground">
              Tasks with probe runs, most recent first.
            </p>
          </div>
          <Button
            type="button"
            size="sm"
            className="h-8 text-xs"
            onClick={() => setPickerOpen(true)}
          >
            + New probe run
          </Button>
        </CardHeader>
        <CardContent>
          {error ? (
            <Alert variant="destructive">
              <AlertTitle>Failed to load probe runs</AlertTitle>
              <AlertDescription>{error instanceof Error ? error.message : String(error)}</AlertDescription>
            </Alert>
          ) : isLoading ? (
            <div className="py-8 text-center text-sm text-muted-foreground">
              Loading…
            </div>
          ) : !data || data.length === 0 ? (
            <div className="rounded-lg border border-dashed border-[#6f88b4]/30 bg-card/60 px-6 py-10 text-center text-sm text-muted-foreground">
              No probe runs yet. Start one with &quot;+ New probe run&quot;.
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
                {data.map((row) => (
                  <TableRow key={row.task_id}>
                    <TableCell className="text-xs font-medium">
                      <Link
                        href={`/tasks/${row.task_id}/probe`}
                        className="hover:underline"
                      >
                        {row.task_name}
                      </Link>
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {row.run_count}
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant="outline"
                        className="text-[10px] px-1.5 py-0 border-[#6f88b4]/30"
                      >
                        {row.last_status}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-[11px] text-muted-foreground">
                      {row.last_run_at
                        ? new Date(row.last_run_at).toLocaleString()
                        : "—"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <NewProbeRunDialog open={pickerOpen} onOpenChange={setPickerOpen} />
    </div>
  );
}
