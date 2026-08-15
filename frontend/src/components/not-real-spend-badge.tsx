"use client";

import { Info } from "lucide-react";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { formatCostUsd } from "@/lib/format";

/** Why a figure on this page is absent from the admin cost dashboards.
 *
 * Operators mark models and experiments whose spend was never really paid for
 * (sponsored capacity, free tiers, a comped run). Pages that price work keep
 * showing that money — it describes what ran — but the cost dashboards and
 * quota sums drop it. Without this marker the two disagree silently, and a
 * reader reconciling them has no way to find out why. */
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
  // Naming the excluded slice only helps when it is a slice — when it is the
  // whole figure, the amount is already on screen right next to this.
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

/** Why one trial's cost isn't real, from its `cost_exclusion_reason`.
 *
 * Null for a real cost — and also for an endpoint that never resolved
 * exclusions, which is why the absence of a badge is not a claim that the
 * spend is real. */
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

/** The trial-level marker, sized to sit inline next to a cost figure. */
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

/** The visible marker for {@link notRealSpendNote}. Renders nothing when the
 * spend is real, so callers can drop it in unconditionally. */
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
