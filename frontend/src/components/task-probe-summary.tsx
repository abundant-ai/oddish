"use client";

import Link from "next/link";
import { ExternalLink } from "lucide-react";
import { type ProbeTrial } from "@/lib/probe-summary";
import { ProbeRunSummary } from "@/components/probe-run-summary";

export function TaskProbeSummary({
  trial,
  taskId,
}: {
  trial: ProbeTrial;
  taskId: string;
}) {
  const hasReward = trial.reward !== null && trial.reward !== undefined;
  const cheatFound = hasReward && (trial.reward as number) >= 0.5;

  return (
    <div className="space-y-4 p-4 sm:p-6">
      {/* Status row */}
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="bg-muted rounded px-2 py-1 font-mono">
          {trial.status}
        </span>
        <span className="text-muted-foreground">
          {trial.agent}
          {trial.model ? ` · ${trial.model}` : ""}
        </span>
        {hasReward ? (
          <span className="text-muted-foreground">
            reward <strong>{(trial.reward as number).toFixed(2)}</strong>
          </span>
        ) : null}
        {hasReward ? (
          cheatFound ? (
            <span className="rounded bg-red-500/15 px-2 py-1 font-medium text-red-600">
              Cheat may have succeeded
            </span>
          ) : (
            <span className="rounded bg-emerald-500/15 px-2 py-1 font-medium text-emerald-700">
              Verifier failed (reward &lt; 0.5)
            </span>
          )
        ) : null}
      </div>

      <ProbeRunSummary
        trial={trial}
        action={
          <Link
            href={`/tasks/${taskId}/probe/${trial.id}`}
            className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1 text-xs underline"
          >
            View full probe run
            <ExternalLink className="h-3 w-3" aria-hidden="true" />
          </Link>
        }
      />
    </div>
  );
}
