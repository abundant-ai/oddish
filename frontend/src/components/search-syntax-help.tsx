"use client";

import { useState, type ReactNode } from "react";
import { CircleHelp } from "lucide-react";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

export function SearchSyntaxRow({
  example,
  hint,
}: {
  example: string;
  hint: string;
}) {
  return (
    <p className="flex items-baseline gap-2">
      <code className="shrink-0 rounded bg-muted px-1 font-mono">{example}</code>
      <span className="text-muted-foreground">{hint}</span>
    </p>
  );
}

/**
 * A trailing "?" help icon for a search input that reveals the search-syntax
 * card on hover AND opens it instantly on click.
 *
 * The plain Radix tooltip only opens after the provider's hover delay, and a
 * click within that window is swallowed (its trigger closes on pointerdown and
 * on click). We make it click-to-open by controlling `open` and intercepting
 * both events with `preventDefault()` -- Radix composes its internal close
 * handlers with `checkForDefaultPrevented`, so a prevented event skips them.
 * Hover and keyboard focus still open it via `onOpenChange`.
 *
 * Renders absolutely positioned, so place it inside a `relative` wrapper next
 * to an `Input` that has trailing padding (e.g. `pr-7`). Self-contained: it
 * carries its own `TooltipProvider`, so it works anywhere.
 */
export function SearchSyntaxHelp({
  children,
  label = "Search syntax help",
}: {
  children: ReactNode;
  label?: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <TooltipProvider delayDuration={200}>
      <Tooltip open={open} onOpenChange={setOpen}>
        <TooltipTrigger asChild>
          <button
            type="button"
            aria-label={label}
            onPointerDown={(event) => {
              // Skip Radix's close-on-pointerdown and open immediately.
              event.preventDefault();
              setOpen(true);
            }}
            onClick={(event) => {
              // Skip Radix's close-on-click so the click that opened it
              // doesn't immediately close it again.
              event.preventDefault();
            }}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground transition-colors hover:text-foreground"
          >
            <CircleHelp className="h-3.5 w-3.5" />
          </button>
        </TooltipTrigger>
        <TooltipContent
          side="bottom"
          align="end"
          className="max-w-[320px] space-y-1.5"
        >
          {children}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
