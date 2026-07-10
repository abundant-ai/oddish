"use client";

import { useState } from "react";
import useSWR from "swr";
import {
  ArrowDown,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Clock,
  Ban,
  Loader2,
  RefreshCw,
  GitBranch,
  Sparkles,
  CircleSlash,
  CircleDashed,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { fetcher } from "@/lib/api";
import { cn } from "@/lib/utils";
import type {
  TrajectoryGraph,
  TrajectoryGraphOutcome,
  TrajectoryGraphStepStatus,
} from "@/lib/types";

interface TrajectoryGraphViewProps {
  trialId: string;
  hasTrajectory?: boolean;
  apiBaseUrl?: string;
}

// GET returns either a stored graph or a not-generated marker.
type GraphResponse = TrajectoryGraph | { status: "not_generated" };

function isGraph(data: GraphResponse | null | undefined): data is TrajectoryGraph {
  return !!data && "steps" in data;
}

const STEP_STYLE: Record<
  TrajectoryGraphStepStatus,
  { ring: string; dot: string; label: string }
> = {
  ok: {
    ring: "border-border bg-card",
    dot: "bg-muted-foreground/40",
    label: "text-foreground",
  },
  warn: {
    ring: "border-amber-500/40 bg-amber-500/5",
    dot: "bg-amber-500",
    label: "text-foreground",
  },
  error: {
    ring: "border-red-500/50 bg-red-500/5",
    dot: "bg-red-500",
    label: "text-foreground",
  },
};

const OUTCOME_STYLE: Record<
  TrajectoryGraphOutcome,
  { ring: string; icon: React.ReactNode; label: string; tone: string }
> = {
  success: {
    ring: "border-emerald-500/50 bg-emerald-500/10",
    icon: <CheckCircle2 className="h-5 w-5 text-emerald-500" />,
    label: "Passed",
    tone: "text-emerald-600 dark:text-emerald-400",
  },
  partial: {
    ring: "border-amber-500/50 bg-amber-500/10",
    icon: <CircleSlash className="h-5 w-5 text-amber-500" />,
    label: "Partial credit",
    tone: "text-amber-600 dark:text-amber-400",
  },
  failure: {
    ring: "border-red-500/50 bg-red-500/10",
    icon: <XCircle className="h-5 w-5 text-red-500" />,
    label: "Failed grader",
    tone: "text-red-600 dark:text-red-400",
  },
  timeout: {
    ring: "border-amber-500/50 bg-amber-500/10",
    icon: <Clock className="h-5 w-5 text-amber-500" />,
    label: "Timed out",
    tone: "text-amber-600 dark:text-amber-400",
  },
  error: {
    ring: "border-slate-500/50 bg-slate-500/10",
    icon: <AlertTriangle className="h-5 w-5 text-slate-500" />,
    label: "Harness error",
    tone: "text-slate-600 dark:text-slate-300",
  },
  scoreless: {
    ring: "border-slate-500/40 bg-slate-500/5",
    icon: <CircleDashed className="h-5 w-5 text-slate-500" />,
    label: "No score",
    tone: "text-slate-600 dark:text-slate-300",
  },
  skipped: {
    ring: "border-slate-500/40 bg-slate-500/5",
    icon: <Ban className="h-5 w-5 text-slate-500" />,
    label: "Skipped",
    tone: "text-slate-600 dark:text-slate-300",
  },
  running: {
    ring: "border-blue-500/50 bg-blue-500/10",
    icon: <Loader2 className="h-5 w-5 animate-spin text-blue-500" />,
    label: "Running",
    tone: "text-blue-600 dark:text-blue-400",
  },
  queued: {
    ring: "border-purple-500/50 bg-purple-500/10",
    icon: <Clock className="h-5 w-5 text-purple-500" />,
    label: "Queued",
    tone: "text-purple-600 dark:text-purple-400",
  },
  pending: {
    ring: "border-gray-500/50 bg-gray-500/10",
    icon: <Clock className="h-5 w-5 text-gray-500" />,
    label: "Pending",
    tone: "text-gray-600 dark:text-gray-400",
  },
};

export function TrajectoryGraphView({
  trialId,
  hasTrajectory,
  apiBaseUrl = "/api",
}: TrajectoryGraphViewProps) {
  const [generating, setGenerating] = useState(false);
  const [genError, setGenError] = useState<string | null>(null);

  const { data, error, isLoading, mutate } = useSWR<GraphResponse | null>(
    `${apiBaseUrl}/trials/${trialId}/trajectory/graph`,
    fetcher,
    { revalidateOnFocus: false },
  );

  const generate = async (refresh: boolean) => {
    setGenerating(true);
    setGenError(null);
    try {
      const res = await fetch(
        `${apiBaseUrl}/trials/${trialId}/trajectory/graph${
          refresh ? "?refresh=true" : ""
        }`,
        { method: "POST", credentials: "include" },
      );
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error(body?.detail || body?.error || `HTTP ${res.status}`);
      }
      const graph = (await res.json()) as TrajectoryGraph;
      await mutate(graph, { revalidate: false });
    } catch (e) {
      setGenError(e instanceof Error ? e.message : "Generation failed");
    } finally {
      setGenerating(false);
    }
  };

  if (isLoading) {
    return (
      <div className="space-y-3 p-4 sm:p-6">
        <Skeleton className="h-6 w-2/3" />
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-16 w-full" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="text-muted-foreground p-6 text-sm">
        Could not load the agent graph for this trial.
      </div>
    );
  }

  // No fetchable trajectory: show the empty state regardless of whether an
  // (outdated) graph is still persisted — a trial with no trajectory shouldn't
  // render a stale graph the user also can't regenerate (backend 404s). Note
  // `hasTrajectory` is the computed _has_fetchable_trajectory value (true for
  // finished Grok Build runs), so this doesn't hide legitimate graphs.
  if (hasTrajectory === false) {
    return (
      <div className="text-muted-foreground flex flex-col items-center gap-2 p-10 text-sm">
        <GitBranch className="h-6 w-6 opacity-50" />
        This trial has no trajectory to summarize.
      </div>
    );
  }

  // Not generated yet: show the manual trigger.
  if (!isGraph(data)) {
    return (
      <div className="flex flex-col items-center gap-3 p-10 text-center">
        <GitBranch className="text-muted-foreground/50 h-7 w-7" />
        <p className="text-muted-foreground max-w-sm text-sm">
          Condense this trial&apos;s trajectory into a graph of the general
          phases the agent moved through and where it ended.
        </p>
        <Button size="sm" onClick={() => generate(false)} disabled={generating}>
          {generating ? (
            <Loader2 className="mr-1.5 h-4 w-4 animate-spin" />
          ) : (
            <Sparkles className="mr-1.5 h-4 w-4" />
          )}
          {generating ? "Generating..." : "Generate agent graph"}
        </Button>
        {genError && <p className="text-xs text-red-500">{genError}</p>}
      </div>
    );
  }

  const graph = data;
  const outcome = OUTCOME_STYLE[graph.terminal.outcome] ?? OUTCOME_STYLE.error;

  return (
    <div className="p-4 sm:p-6">
      {/* Header */}
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <p className="text-foreground text-sm leading-relaxed font-medium">
            {graph.headline}
          </p>
          <div className="text-muted-foreground mt-1.5 flex flex-wrap items-center gap-2 text-[11px]">
            <Badge variant="outline" className={cn("gap-1", outcome.tone)}>
              {outcome.label}
            </Badge>
            <span>
              {graph.steps.length} phase{graph.steps.length !== 1 ? "s" : ""}
            </span>
            {graph.num_steps != null && (
              <span>· {graph.num_steps} trajectory steps</span>
            )}
            <span>· {graph.source === "heuristic" ? "heuristic" : "AI summary"}</span>
          </div>
        </div>
        <Button
          variant="ghost"
          size="sm"
          className="h-7 gap-1 text-xs"
          onClick={() => generate(true)}
          disabled={generating}
        >
          <RefreshCw className={cn("h-3.5 w-3.5", generating && "animate-spin")} />
          Rebuild
        </Button>
      </div>

      {genError && <p className="mb-3 text-xs text-red-500">{genError}</p>}

      {/* Flow graph */}
      <div className="mx-auto flex max-w-2xl flex-col items-stretch">
        {graph.steps.map((step, i) => {
          const style = STEP_STYLE[step.status] ?? STEP_STYLE.ok;
          return (
            <div key={step.id ?? i} className="flex flex-col items-center">
              <Card className={cn("w-full border", style.ring)}>
                <CardContent className="flex items-start gap-3 px-4 py-3">
                  <span className="text-muted-foreground/60 mt-0.5 font-mono text-[11px]">
                    {i + 1}
                  </span>
                  <span
                    className={cn(
                      "mt-1.5 h-2 w-2 shrink-0 rounded-full",
                      style.dot,
                    )}
                  />
                  <div className="min-w-0 flex-1">
                    <p className={cn("text-sm font-semibold", style.label)}>
                      {step.title}
                    </p>
                    {step.detail && (
                      <p className="text-muted-foreground mt-0.5 text-xs leading-relaxed">
                        {step.detail}
                      </p>
                    )}
                  </div>
                </CardContent>
              </Card>
              <ArrowDown className="text-muted-foreground/40 my-1 h-4 w-4 shrink-0" />
            </div>
          );
        })}

        {/* Terminal node */}
        <Card className={cn("w-full border-2", outcome.ring)}>
          <CardContent className="px-4 py-3">
            <div className="flex items-start gap-3">
              <span className="mt-0.5">{outcome.icon}</span>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-baseline gap-2">
                  <span className={cn("text-sm font-bold", outcome.tone)}>
                    {outcome.label}
                  </span>
                  <span className="text-muted-foreground text-xs">
                    {graph.terminal.last_action}
                  </span>
                </div>
                {graph.terminal.reason && (
                  <p className="text-muted-foreground/90 mt-1 text-xs leading-relaxed">
                    {graph.terminal.reason}
                  </p>
                )}
              </div>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
