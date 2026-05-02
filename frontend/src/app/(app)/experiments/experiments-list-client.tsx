"use client";

import Link from "next/link";
import useSWR from "swr";
import { Plus } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { fetcher } from "@/lib/api";
import type { ExperimentListItem } from "@/lib/types";
import { encodeExperimentRouteParam, formatRelativeTime } from "@/lib/utils";

export function ExperimentsListClient() {
  const { data, error, isLoading } = useSWR<ExperimentListItem[]>(
    "/api/experiments?limit=200",
    fetcher,
    { refreshInterval: 30000, revalidateOnFocus: false }
  );

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="font-mono text-2xl font-semibold tracking-tight">
            Experiments
          </h1>
          <p className="text-muted-foreground mt-1 max-w-3xl text-sm">
            Saved selections of task versions and agents. Open one to inspect
            its matrix, edit cells, and backfill gaps.
          </p>
        </div>
        <Button asChild>
          <Link href="/experiments/new">
            <Plus className="mr-2 h-4 w-4" />
            New experiment
          </Link>
        </Button>
      </div>

      <Card className="border-[#6f88b4]/20 shadow-xs">
        <CardHeader>
          <CardTitle className="text-base">Saved Experiments</CardTitle>
        </CardHeader>
        <CardContent>
          {error ? (
            <div className="border-destructive/40 bg-destructive/5 rounded-md border p-3 text-sm">
              Failed to load experiments: {String((error as Error).message)}
            </div>
          ) : isLoading && !data ? (
            <Skeleton className="h-64 w-full" />
          ) : !data || data.length === 0 ? (
            <div className="bg-card/60 text-muted-foreground rounded-lg border border-dashed border-[#6f88b4]/30 px-6 py-10 text-center text-sm">
              No experiments yet. Create one from task versions and agents.
            </div>
          ) : (
            <div className="border-border/70 overflow-x-auto rounded-lg border">
              <table className="min-w-full text-sm">
                <thead>
                  <tr className="bg-muted/40 text-muted-foreground text-left text-[11px] tracking-wide uppercase">
                    <th className="px-3 py-2 font-medium">Name</th>
                    <th className="px-3 py-2 font-medium">Cells</th>
                    <th className="px-3 py-2 font-medium">Visibility</th>
                    <th className="px-3 py-2 font-medium">Created</th>
                    <th className="px-3 py-2 text-right font-medium">ID</th>
                  </tr>
                </thead>
                <tbody>
                  {data.map((experiment) => (
                    <tr
                      key={experiment.id}
                      className="border-border/70 border-t"
                    >
                      <td className="px-3 py-3">
                        <Link
                          href={`/experiments/${encodeExperimentRouteParam(
                            experiment.id
                          )}`}
                          className="font-medium text-[#5d77a5] underline-offset-4 hover:underline dark:text-[#a8b8d2]"
                        >
                          {experiment.name}
                        </Link>
                      </td>
                      <td className="px-3 py-3 font-mono">
                        {experiment.cell_count}
                      </td>
                      <td className="px-3 py-3">
                        <Badge
                          variant={experiment.is_public ? "default" : "outline"}
                        >
                          {experiment.is_public ? "Public" : "Private"}
                        </Badge>
                      </td>
                      <td className="text-muted-foreground px-3 py-3 text-xs">
                        {formatRelativeTime(experiment.created_at)}
                      </td>
                      <td className="text-muted-foreground px-3 py-3 text-right font-mono text-[11px]">
                        {experiment.id}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
