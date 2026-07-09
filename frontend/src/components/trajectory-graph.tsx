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
  Copy,
  Check,
  GitBranch,
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
};

// Escape a label for a mermaid node body (quotes, brackets, newlines break the parse).
function mmLabel(text: string): string {
  return (text || "")
    .replace(/["[\]{}|]/g, "")
    .replace(/\n+/g, " ")
    .slice(0, 80);
}

function buildMermaid(graph: TrajectoryGraph): string {
  const lines: string[] = ["flowchart TD"];
  const nodeIds: string[] = [];
  graph.steps.forEach((s, i) => {
    const id = `s${i}`;
    nodeIds.push(id);
    lines.push(`  ${id}["${mmLabel(s.title)}"]`);
  });
  const t = graph.terminal;
  const termLabel = mmLabel(
    `${OUTCOME_STYLE[t.outcome]?.label ?? t.outcome}: ${t.last_action}`,
  );
  lines.push(`  term{{"${termLabel}"}}`);
  const chain = [...nodeIds, "term"];
  for (let i = 0; i < chain.length - 1; i++) {
    lines.push(`  ${chain[i]} --> ${chain[i + 1]}`);
  }
  // Status styling.
  graph.steps.forEach((s, i) => {
    if (s.status === "warn") lines.push(`  class s${i} warn`);
    if (s.status === "error") lines.push(`  class s${i} err`);
  });
  const termClass =
    t.outcome === "success"
      ? "pass"
      : t.outcome === "timeout" || t.outcome === "running"
        ? "warn"
        : "err";
  lines.push(`  class term ${termClass}`);
  lines.push("  classDef warn fill:#78350f22,stroke:#f59e0b");
  lines.push("  classDef err fill:#7f1d1d22,stroke:#ef4444");
  lines.push("  classDef pass fill:#064e3b22,stroke:#10b981");
  return lines.join("\n");
}

export function TrajectoryGraphView({
  trialId,
  hasTrajectory,
  apiBaseUrl = "/api",
}: TrajectoryGraphViewProps) {
  const [refreshing, setRefreshing] = useState(false);
  const [copied, setCopied] = useState(false);

  const { data, error, isLoading, mutate } = useSWR<TrajectoryGraph | null>(
    `${apiBaseUrl}/trials/${trialId}/trajectory/graph`,
    fetcher,
    { revalidateOnFocus: false },
  );

  const handleRefresh = async () => {
    setRefreshing(true);
    try {
      const res = await fetcher<TrajectoryGraph>(
        `${apiBaseUrl}/trials/${trialId}/trajectory/graph?refresh=true`,
      );
      await mutate(res, { revalidate: false });
    } catch {
      // keep the current graph on failure
    } finally {
      setRefreshing(false);
    }
  };

  const handleCopyMermaid = async () => {
    if (!data) return;
    try {
      await navigator.clipboard.writeText(buildMermaid(data));
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard blocked; ignore
    }
  };

  if (isLoading) {
    return (
      <div className="space-y-3 p-4 sm:p-6">
        <Skeleton className="h-6 w-2/3" />
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-16 w-full" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="text-muted-foreground p-6 text-sm">
        Could not build the agent graph for this trial.
      </div>
    );
  }

  if (!data || !data.steps?.length) {
    return (
      <div className="text-muted-foreground flex flex-col items-center gap-2 p-10 text-sm">
        <GitBranch className="h-6 w-6 opacity-50" />
        {hasTrajectory === false
          ? "This trial has no trajectory to summarize."
          : "No agent graph available yet."}
      </div>
    );
  }

  const outcome = OUTCOME_STYLE[data.terminal.outcome] ?? OUTCOME_STYLE.error;

  return (
    <div className="p-4 sm:p-6">
      {/* Header */}
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <p className="text-foreground text-sm leading-relaxed font-medium">
            {data.headline}
          </p>
          <div className="text-muted-foreground mt-1.5 flex flex-wrap items-center gap-2 text-[11px]">
            <Badge variant="outline" className={cn("gap-1", outcome.tone)}>
              {outcome.label}
            </Badge>
            <span>
              {data.steps.length} phase{data.steps.length !== 1 ? "s" : ""}
            </span>
            {data.num_steps != null && (
              <span>· {data.num_steps} trajectory steps</span>
            )}
            <span>· {data.source === "heuristic" ? "heuristic" : "AI summary"}</span>
          </div>
        </div>
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="sm"
            className="h-7 gap-1 text-xs"
            onClick={handleCopyMermaid}
          >
            {copied ? (
              <Check className="h-3.5 w-3.5" />
            ) : (
              <Copy className="h-3.5 w-3.5" />
            )}
            Mermaid
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="h-7 gap-1 text-xs"
            onClick={handleRefresh}
            disabled={refreshing}
          >
            <RefreshCw
              className={cn("h-3.5 w-3.5", refreshing && "animate-spin")}
            />
            Rebuild
          </Button>
        </div>
      </div>

      {/* Flow graph */}
      <div className="mx-auto flex max-w-2xl flex-col items-stretch">
        {data.steps.map((step, i) => {
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
                    {data.terminal.last_action}
                  </span>
                </div>
                {data.terminal.reason && (
                  <p className="text-muted-foreground/90 mt-1 text-xs leading-relaxed">
                    {data.terminal.reason}
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
