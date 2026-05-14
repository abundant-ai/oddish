"use client";

import { useState, useEffect, useRef } from "react";
import { PanelRightClose, PanelRightOpen } from "lucide-react";
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
  /**
   * Render the trial detail. Receives a `paneAction` slot that should be
   * placed in the trial header — it's the toggle that hides/shows the task
   * pane from within the trial view.
   */
  renderTrial?: (paneAction: React.ReactNode) => React.ReactNode;
  /** Convenience fallback when there's no paneAction slot (e.g. task mode). */
  trialContent?: React.ReactNode;
  /** Show the task-files (left) pane in trial mode. Default true. */
  showTask?: boolean;
  /** Show the trial-detail (right) pane in trial mode. Default true. */
  showTrial?: boolean;
  onShowTaskChange?: (next: boolean) => void;
  onShowTrialChange?: (next: boolean) => void;
  /** Content for the left pane (typically a task file viewer). */
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
  renderTrial,
  trialContent,
  showTask = true,
  showTrial = true,
  onShowTaskChange,
  onShowTrialChange,
  sideBySideLeft,
  defaultWidth = 1080,
  sideBySideWidth = 1500,
  minWidth = 420,
  maxWidth = 1800,
}: UnifiedDrawerWrapperProps) {
  const [displayMode, setDisplayMode] = useState<DrawerMode>(mode);
  const [isTransitioning, setIsTransitioning] = useState(false);
  const previousMode = useRef<DrawerMode>(mode);

  const hasLeft = Boolean(sideBySideLeft);
  const sideBySideActive =
    displayMode === "trial" && showTask && showTrial && hasLeft;
  const taskOnlyActive =
    displayMode === "trial" && showTask && hasLeft && !showTrial;

  const [width, setWidth] = useState(
    sideBySideActive ? sideBySideWidth : defaultWidth
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

  // The task-def pane carries the "trials" toggle — clicking it expands or
  // collapses the trial pane on its right. Disabled when toggling would
  // collapse everything.
  const trialsToggle = onShowTrialChange ? (
    <Button
      type="button"
      size="sm"
      variant="ghost"
      className="text-muted-foreground hover:text-foreground h-7 gap-1 px-2 text-[10px] font-semibold tracking-wide uppercase"
      onClick={() => onShowTrialChange(!showTrial)}
      disabled={showTrial && !showTask}
      aria-pressed={!showTrial}
      title={showTrial ? "Hide trials pane" : "Show trials pane"}
    >
      {showTrial ? (
        <PanelRightClose className="h-3.5 w-3.5" />
      ) : (
        <PanelRightOpen className="h-3.5 w-3.5" />
      )}
      <span className="hidden sm:inline">
        {showTrial ? "Hide trials" : "Show trials"}
      </span>
    </Button>
  ) : null;

  // Lives in the trial pane header — controls the task-def pane on its left.
  const taskToggle = onShowTaskChange ? (
    <Button
      type="button"
      size="sm"
      variant="ghost"
      className="text-muted-foreground hover:text-foreground h-7 gap-1 px-2 text-[10px] font-semibold tracking-wide uppercase"
      onClick={() => onShowTaskChange(!showTask)}
      disabled={showTask && !showTrial}
      aria-pressed={!showTask}
      title={
        showTask ? "Hide task definition pane" : "Show task definition pane"
      }
    >
      {showTask ? (
        // Mirror of PanelRightClose for the left pane.
        <PanelRightClose className="h-3.5 w-3.5 -scale-x-100" />
      ) : (
        <PanelRightOpen className="h-3.5 w-3.5 -scale-x-100" />
      )}
      <span className="hidden sm:inline">
        {showTask ? "Hide task" : "Show task"}
      </span>
    </Button>
  ) : null;

  const taskFilesPane = (
    <div className="bg-background flex h-full flex-col overflow-hidden">
      <div className="border-border bg-muted/40 flex h-10 shrink-0 items-center justify-between gap-2 border-b px-2 sm:h-12 sm:px-3">
        <span className="text-muted-foreground pl-2 text-[10px] font-semibold tracking-wider uppercase">
          Task definition
        </span>
        {trialsToggle}
      </div>
      <div className="flex flex-1 flex-col overflow-hidden">
        {sideBySideLeft}
      </div>
    </div>
  );

  const renderedTrial = renderTrial
    ? renderTrial(taskToggle)
    : (trialContent ?? null);

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
          {taskFilesPane}
        </ResizablePanel>
        <ResizableHandle withHandle />
        <ResizablePanel defaultSize={58} minSize={30}>
          <div className="flex h-full flex-col overflow-hidden">
            {renderedTrial}
          </div>
        </ResizablePanel>
      </ResizablePanelGroup>
    ) : taskOnlyActive ? (
      taskFilesPane
    ) : (
      <div className="flex h-full flex-col overflow-hidden">
        {renderedTrial}
      </div>
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
      <div
        className={cn(
          "flex flex-1 flex-col overflow-hidden transition-opacity duration-300"
        )}
        style={{ opacity: isTransitioning ? 0.3 : 1 }}
      >
        {body}
      </div>
    </ResizableDrawer>
  );
}
