"use client";

import useSWR from "swr";
import Link from "next/link";
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
import { encodeExperimentRouteParam } from "@/lib/utils";

interface ExperimentSummary {
  id: string;
  name: string;
  is_public: boolean;
  created_at: string | null;
}

function rel(iso: string | null): string {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export function ExperimentsListClient() {
  const { data, error, isLoading } = useSWR<ExperimentSummary[]>(
    "/api/experiments?limit=200",
    fetcher,
    { refreshInterval: 30_000 },
  );

  return (
    <div className="mx-auto w-full max-w-(--breakpoint-2xl) space-y-4 p-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-mono text-[26px] font-semibold tracking-[-0.02em]">
            Experiments
          </h1>
        </div>
        <Button asChild>
          <Link href="/experiments/new" className="gap-2">
            <Plus className="h-4 w-4" /> New experiment
          </Link>
        </Button>
      </div>

      {error ? (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
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
                <TableHead>Visibility</TableHead>
                <TableHead>Created</TableHead>
                <TableHead className="text-right">ID</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {!data || data.length === 0 ? (
                <TableRow>
                  <TableCell
                    colSpan={4}
                    className="text-center text-sm text-muted-foreground"
                  >
                    No experiments yet. Click "New experiment" to create one.
                  </TableCell>
                </TableRow>
              ) : (
                data.map((exp) => (
                  <TableRow key={exp.id}>
                    <TableCell>
                      <Link
                        href={`/experiments/${encodeExperimentRouteParam(exp.id)}`}
                        className="font-medium underline-offset-2 hover:underline"
                      >
                        {exp.name}
                      </Link>
                    </TableCell>
                    <TableCell>
                      <Badge variant={exp.is_public ? "default" : "outline"}>
                        {exp.is_public ? "Public" : "Private"}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {rel(exp.created_at)}
                    </TableCell>
                    <TableCell className="text-right font-mono text-[11px] text-muted-foreground">
                      {exp.id}
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
