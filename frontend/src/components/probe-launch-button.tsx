"use client";

import Link from "next/link";
import { SearchCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";

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
