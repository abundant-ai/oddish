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
      {description ? (
        <TooltipProvider delayDuration={150}>
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                className={labelClass}
                aria-label={`How ${label} is calculated`}
              >
                {label}
                <Info className="h-3 w-3" />
              </button>
            </TooltipTrigger>
            <TooltipContent className="max-w-xs">{description}</TooltipContent>
          </Tooltip>
        </TooltipProvider>
      ) : (
        <span className={labelClass}>{label}</span>
      )}
      <span className="flex min-w-0 flex-wrap items-baseline gap-1.5 font-sans text-[28px] leading-tight font-medium text-[color:var(--paper-ink)] tabular-nums">
        {children}
      </span>
      {hint ? (
        <div className="text-xs text-[color:var(--paper-ink-2)]">{hint}</div>
      ) : null}
    </div>
  );
}
