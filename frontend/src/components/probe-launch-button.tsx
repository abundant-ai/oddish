"use client";

import { useState } from "react";
import { SearchCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { ProbeSubmitForm } from "@/components/probe-submit-form";

// Opens a modal with the same submit form as the standalone probe page,
// scoped to a single task. `variant="icon"` renders a tooltip'd icon button
// (requires a TooltipProvider ancestor); `variant="labeled"` renders an
// icon + label button for use in page headers.
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
  const [open, setOpen] = useState(false);

  const trigger =
    variant === "icon" ? (
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className={className}
        aria-label={`Launch probe for ${taskName}`}
      >
        <SearchCheck className="h-3.5 w-3.5" />
      </Button>
    ) : (
      <Button
        type="button"
        variant="ghost"
        className={className}
        aria-label={`Launch probe for ${taskName}`}
      >
        <SearchCheck className="h-3.5 w-3.5" />
        {label}
      </Button>
    );

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      {variant === "icon" ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <DialogTrigger asChild>{trigger}</DialogTrigger>
          </TooltipTrigger>
          <TooltipContent>{label}</TooltipContent>
        </Tooltip>
      ) : (
        <DialogTrigger asChild>{trigger}</DialogTrigger>
      )}
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>New probe</DialogTitle>
        </DialogHeader>
        <ProbeSubmitForm taskId={taskId} onSubmitted={() => setOpen(false)} />
      </DialogContent>
    </Dialog>
  );
}
