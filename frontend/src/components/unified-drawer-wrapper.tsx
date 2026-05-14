"use client";

import { useState, useEffect, useRef } from "react";
import { Columns2, PanelLeftClose } from "lucide-react";
import { ResizableDrawer } from "@/components/ui/resizable-drawer";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

type DrawerMode = "task" | "trial";

interface UnifiedDrawerWrapperProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  mode: DrawerMode;
  taskContent: React.ReactNode;
  trialContent: React.ReactNode;
  /** When true, render the side panel with task files alongside the trial detail. */
  sideBySide?: boolean;
  /** Toggle side-by-side mode (when omitted, the toggle button is hidden). */
  onSideBySideChange?: (next: boolean) => void;
  /** Content for the left pane when side-by-side is active (typically a task file viewer). */
  sideBySideLeft?: React.ReactNode;
  defaultWidth?: number;
  sideBySideWidth?: number;
  minWidth?: number;
  maxWidth?: number;
}

export function UnifiedDrawerWrapper({
  open,
  onOpenChange,
  mode,
  taskContent,
  trialContent,
  sideBySide = false,
  onSideBySideChange,
  sideBySideLeft,
  defaultWidth = 1080,
  sideBySideWidth = 1500,
  minWidth = 420,
  maxWidth = 1800,
}: UnifiedDrawerWrapperProps) {
  const [displayMode, setDisplayMode] = useState<DrawerMode>(mode);
  const [isTransitioning, setIsTransitioning] = useState(false);
  const previousMode = useRef<DrawerMode>(mode);

  const [width, setWidth] = useState(
    sideBySide ? sideBySideWidth : defaultWidth
  );
  const userResizedRef = useRef(false);

  // Smooth crossfade between task/trial mode swaps.
  useEffect(() => {
    if (mode !== previousMode.current && open) {
      setIsTransitioning(true);
      const timer = setTimeout(() => {
        setDisplayMode(mode);
        setIsTransitioning(false);
        previousMode.current = mode;
      }, 150);
      return () => clearTimeout(timer);
    } else if (!open) {
      setDisplayMode(mode);
      previousMode.current = mode;
    }
  }, [mode, open]);

  // Only activate the actual two-pane layout in trial mode — in task mode the
  // detail panel itself already shows the task files, so a duplicate left pane
  // would be wasted space. The toggle button still appears so users can grow
  // / shrink the drawer.
  const sideBySideActive =
    sideBySide && displayMode === "trial" && Boolean(sideBySideLeft);

  // Auto-grow / shrink the drawer when side-by-side toggles, unless the user
  // has manually resized — then we keep their width.
  useEffect(() => {
    if (userResizedRef.current) return;
    setWidth(sideBySideActive ? sideBySideWidth : defaultWidth);
  }, [sideBySideActive, sideBySideWidth, defaultWidth]);

  const handleWidthChange = (next: number) => {
    userResizedRef.current = true;
    setWidth(next);
  };

  const body =
    displayMode === "task" ? (
      <div className="flex h-full flex-col overflow-hidden">{taskContent}</div>
    ) : sideBySideActive ? (
      <ResizablePanelGroup
        direction="horizontal"
        autoSaveId="trial-detail-side-by-side"
        className="h-full"
      >
        <ResizablePanel defaultSize={42} minSize={20} maxSize={70}>
          <div className="bg-background flex h-full flex-col overflow-hidden">
            <div className="border-border bg-muted/40 text-muted-foreground flex h-10 shrink-0 items-center border-b px-4 text-[10px] font-semibold tracking-wider uppercase sm:h-12">
              Task definition
            </div>
            <div className="flex flex-1 flex-col overflow-hidden">
              {sideBySideLeft}
            </div>
          </div>
        </ResizablePanel>
        <ResizableHandle withHandle />
        <ResizablePanel defaultSize={58} minSize={30}>
          <div className="flex h-full flex-col overflow-hidden">
            {trialContent}
          </div>
        </ResizablePanel>
      </ResizablePanelGroup>
    ) : (
      <div className="flex h-full flex-col overflow-hidden">{trialContent}</div>
    );

  return (
    <ResizableDrawer
      open={open}
      onOpenChange={onOpenChange}
      defaultWidth={defaultWidth}
      minWidth={minWidth}
      maxWidth={maxWidth}
      width={width}
      onWidthChange={handleWidthChange}
    >
      {onSideBySideChange && displayMode === "trial" && (
        <div className="absolute top-3 right-14 z-20">
          <Button
            type="button"
            size="sm"
            variant={sideBySide ? "default" : "outline"}
            className="h-7 px-2 text-[10px] font-semibold tracking-wide uppercase"
            onClick={() => onSideBySideChange(!sideBySide)}
            aria-pressed={sideBySide}
            title={
              sideBySide
                ? "Hide task files pane"
                : "Show task files side-by-side"
            }
          >
            {sideBySide ? (
              <PanelLeftClose className="mr-1 h-3.5 w-3.5" />
            ) : (
              <Columns2 className="mr-1 h-3.5 w-3.5" />
            )}
            <span className="hidden sm:inline">
              {sideBySide ? "Hide task" : "Task files"}
            </span>
          </Button>
        </div>
      )}
      <div
        className={cn(
          "flex h-full flex-1 flex-col overflow-hidden transition-opacity duration-300"
        )}
        style={{ opacity: isTransitioning ? 0.3 : 1 }}
      >
        {body}
      </div>
    </ResizableDrawer>
  );
}
