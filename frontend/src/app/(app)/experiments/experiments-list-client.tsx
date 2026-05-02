"use client";

import Link from "next/link";
import useSWR from "swr";
import { Plus } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { fetcher } from "@/lib/api";
import type { ExperimentListItem } from "@/lib/types";
import { encodeExperimentRouteParam } from "@/lib/utils";

function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

export function ExperimentsListClient() {
  const { data, error, isLoading } = useSWR<ExperimentListItem[]>(
    "/api/experiments?limit=200",
    fetcher,
    { refreshInterval: 30000, revalidateOnFocus: false }
  );

  return (
    <div className="mx-auto w-full max-w-(--breakpoint-2xl) space-y-4 p-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-mono text-[26px] font-semibold tracking-[-0.02em]">
            Experiments
          </h1>
          <p className="text-muted-foreground text-sm">
            Saved selections of task versions and agents. An experiment is a
            view over evidence; clicking one opens its cell matrix.
          </p>
        </div>
        <Button asChild>
          <Link href="/experiments/new" className="gap-2">
            <Plus className="h-4 w-4" /> New experiment
          </Link>
        </Button>
      </div>

      {error ? (
        <div className="border-destructive/40 bg-destructive/5 rounded-md border p-3 text-sm">
          Failed to load experiments: {String((error as Error).message)}
        </div>
      ) : null}

      {isLoading && !data ? (
        <Skeleton className="h-64 w-full" />
      ) : (
        <div className="overflow-x-auto rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Cells</TableHead>
                <TableHead>Visibility</TableHead>
                <TableHead>Created</TableHead>
                <TableHead className="text-right">ID</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {!data || data.length === 0 ? (
                <TableRow>
                  <TableCell
                    colSpan={5}
                    className="text-muted-foreground text-center text-sm"
                  >
                    No experiments yet. Click &quot;New experiment&quot; to
                    create one.
                  </TableCell>
                </TableRow>
              ) : (
                data.map((experiment) => (
                  <TableRow key={experiment.id}>
                    <TableCell>
                      <Link
                        href={`/experiments/${encodeExperimentRouteParam(
                          experiment.id
                        )}`}
                        className="font-medium underline-offset-2 hover:underline"
                      >
                        {experiment.name}
                      </Link>
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {experiment.cell_count}
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant={experiment.is_public ? "default" : "outline"}
                      >
                        {experiment.is_public ? "Public" : "Private"}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-muted-foreground text-xs">
                      {relativeTime(experiment.created_at)}
                    </TableCell>
                    <TableCell className="text-muted-foreground text-right font-mono text-[11px]">
                      {experiment.id}
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
