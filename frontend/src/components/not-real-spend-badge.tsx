"use client";

import { Info } from "lucide-react";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { formatCostUsd } from "@/lib/format";

export function notRealSpendNote({
  excludedCostUsd,
  totalCostUsd,
  wholeSubjectExcluded = false,
}: {
  excludedCostUsd: number;
  totalCostUsd?: number;
  wholeSubjectExcluded?: boolean;
}): string | null {
  if (wholeSubjectExcluded) {
    return (
      "This experiment is marked as not real spend, so none of this money " +
      "counts: it is excluded from the admin cost dashboards and from quota " +
      "enforcement. The figure still shows what the work would have cost."
    );
  }
  if (!(excludedCostUsd > 0)) return null;
  const partial =
    typeof totalCostUsd === "number" && totalCostUsd > excludedCostUsd;
  return partial
    ? `${formatCostUsd(excludedCostUsd)} of this is not real spend — it ran on ` +
        "a model or in an experiment an admin marked as free. That part is " +
        "excluded from the admin cost dashboards and from quota enforcement."
    : "This is not real spend — it ran on a model or in an experiment an " +
        "admin marked as free. It is excluded from the admin cost dashboards " +
        "and from quota enforcement.";
}

export function trialNotRealSpendNote(reason?: string | null): string | null {
  if (!reason) return null;
  const because =
    reason === "model"
      ? "it ran on a model an admin marked as free"
      : reason === "experiment"
        ? "its experiment is marked as free"
        : "an admin marked it as free";
  return (
    `This isn't real spend — ${because}. The cost is still shown because the ` +
    "work ran, but it is excluded from the admin cost dashboards and from " +
    "quota enforcement."
  );
}

export function TrialNotRealSpendBadge({
  reason,
  className = "",
}: {
  reason?: string | null;
  className?: string;
}) {
  const note = trialNotRealSpendNote(reason);
  if (!note) return null;

  return (
    <TooltipProvider delayDuration={150}>
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            className={`cursor-help font-mono text-[color:var(--paper-ink-3)] ${className}`}
            aria-label="This is not real spend"
          >
            †
          </span>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs normal-case">{note}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export function NotRealSpendBadge({
  excludedCostUsd,
  totalCostUsd,
  wholeSubjectExcluded = false,
  className = "",
}: {
  excludedCostUsd: number;
  totalCostUsd?: number;
  wholeSubjectExcluded?: boolean;
  className?: string;
}) {
  const note = notRealSpendNote({
    excludedCostUsd,
    totalCostUsd,
    wholeSubjectExcluded,
  });
  if (!note) return null;

  return (
    <TooltipProvider delayDuration={150}>
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            className={`inline-flex cursor-help items-center gap-0.5 rounded-sm border border-[color:var(--paper-line-2)] px-1 font-mono text-[10px] leading-none text-[color:var(--paper-ink-3)] ${className}`}
            aria-label="Part of this spend is not real"
          >
            <Info className="h-2.5 w-2.5" aria-hidden="true" />
            not real
          </span>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs normal-case">{note}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
