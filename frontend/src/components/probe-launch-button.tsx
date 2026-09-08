"use client";

import Link from "next/link";
import { SearchCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import type { Task } from "@/lib/types";

// An experiment-level probe runs in the experiment's current/most-recent
// task environment. Prefer the highest current_version, tie-break on most
// recent created_at, fall back to the first task.
export function resolveProbeHostTask(tasks: Task[]): Task | undefined {
  if (tasks.length === 0) return undefined;
  const sorted = [...tasks].sort((a, b) => {
    const v = (b.current_version ?? -1) - (a.current_version ?? -1);
    if (v !== 0) return v;
    return (b.created_at ?? "").localeCompare(a.created_at ?? "");
  });
  return sorted[0];
}

// Navigates to the standalone probe page for a single task. `variant="icon"`
// renders a tooltip'd icon button (requires a TooltipProvider ancestor);
// `variant="labeled"` renders an icon + label button for use in page headers.
export function ProbeLaunchButton({
  taskId,
  taskName,
  variant = "icon",
  className,
  label = "Launch probe",
}: {
  taskId: string;
  taskName: string;
  variant?: "icon" | "labeled";
  className?: string;
  label?: string;
}) {
  const href = `/tasks/${taskId}/probe`;

  if (variant === "labeled") {
    return (
      <Button asChild variant="ghost" className={className}>
        <Link href={href} aria-label={`Launch probe for ${taskName}`}>
          <SearchCheck className="h-3.5 w-3.5" />
          {label}
        </Link>
      </Button>
    );
  }

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button asChild variant="ghost" size="icon" className={className}>
          <Link href={href} aria-label={`Launch probe for ${taskName}`}>
            <SearchCheck className="h-3.5 w-3.5" />
          </Link>
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}
