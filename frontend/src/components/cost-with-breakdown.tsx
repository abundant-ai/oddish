"use client";

import { Info } from "lucide-react";

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { formatCostUsd, formatCostUsdExact } from "@/lib/format";

interface CostWithBreakdownProps {
  /** Composite total to display as the headline number. */
  total: number;
  /** Inference (agent LLM) spend — the trial's own cost_usd. */
  inference?: number | null;
  /** Analysis/QA LLM spend attributed to the trial. */
  qa?: number | null;
  /** Modal compute (sandbox) spend attributed to the trial. */
  compute?: number | null;
  /** When true, prefixes the headline with "~" (token-estimated inference). */
  estimated?: boolean | null;
  className?: string;
}

/**
 * Renders a trial's composite cost as `formatCostUsd(total)` with an ⓘ tooltip
 * breaking it down into exact inference · QA · sandbox components.
 *
 * Wraps its own TooltipProvider so it works anywhere, even outside an existing
 * provider (nested providers are safe).
 */
export function CostWithBreakdown({
  total,
  inference,
  qa,
  compute,
  estimated,
  className,
}: CostWithBreakdownProps) {
  const inf = inference ?? 0;
  const qaCost = qa ?? 0;
  const computeCost = compute ?? 0;
  return (
    <TooltipProvider>
      <span className={className}>
        {estimated ? "~" : ""}
        {formatCostUsd(total)}
        <Tooltip>
          <TooltipTrigger asChild>
            <Info className="text-muted-foreground ml-1 inline h-3 w-3 cursor-help align-text-top" />
          </TooltipTrigger>
          <TooltipContent>
            {formatCostUsdExact(inf)} inference · {formatCostUsdExact(qaCost)}{" "}
            QA · {formatCostUsdExact(computeCost)} sandbox
          </TooltipContent>
        </Tooltip>
      </span>
    </TooltipProvider>
  );
}
