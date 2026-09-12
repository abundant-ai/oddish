import type { ReactNode } from "react";
import { Info } from "lucide-react";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

export function SummaryStat({
  label,
  description,
  children,
  hint,
}: {
  label: string;
  description?: string;
  children: ReactNode;
  hint?: ReactNode;
}) {
  const labelClass =
    "inline-flex items-center gap-1 font-mono text-[11px] text-[color:var(--paper-ink-3)]";
  return (
    <div className="flex min-w-0 flex-col items-start gap-1.5">
      <div className={labelClass}>
        <span>{label}</span>
        {description ? (
          <TooltipProvider delayDuration={150}>
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  className="rounded-sm focus-visible:outline-2 focus-visible:outline-offset-2"
                  aria-label={`How ${label} is calculated`}
                >
                  <Info className="h-3 w-3" aria-hidden="true" />
                </button>
              </TooltipTrigger>
              <TooltipContent className="max-w-xs">
                {description}
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        ) : null}
      </div>
      <span className="flex min-w-0 flex-wrap items-baseline gap-1.5 font-sans text-[28px] leading-tight font-medium text-[color:var(--paper-ink)] tabular-nums">
        {children}
      </span>
      {hint ? (
        <div className="text-xs text-[color:var(--paper-ink-2)]">{hint}</div>
      ) : null}
    </div>
  );
}
