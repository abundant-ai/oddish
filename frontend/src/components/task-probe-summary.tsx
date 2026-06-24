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
  return (
    <div className="space-y-4 p-4 sm:p-6">
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
