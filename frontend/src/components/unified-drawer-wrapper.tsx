"use client";

import { useState, useEffect, useRef } from "react";
import { PanelLeftClose, PanelRightClose, Columns2 } from "lucide-react";
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

  // In trial mode, both panes show by default → side-by-side.
  const hasLeft = Boolean(sideBySideLeft);
  const sideBySideActive =
    displayMode === "trial" && showTask && showTrial && hasLeft;
  const trialOnlyActive =
    displayMode === "trial" && showTrial && !(showTask && hasLeft);
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

  // Don't let the user collapse both panes — keep at least one visible.
  const handleToggleTask = () => {
    if (!onShowTaskChange) return;
    if (showTask) {
      // about to hide task — only allow if trial pane will still be shown
      if (showTrial) onShowTaskChange(false);
    } else {
      onShowTaskChange(true);
    }
  };
  const handleToggleTrial = () => {
    if (!onShowTrialChange) return;
    if (showTrial) {
      if (showTask && hasLeft) onShowTrialChange(false);
    } else {
      onShowTrialChange(true);
    }
  };

  const taskFilesPane = (
    <div className="bg-background flex h-full flex-col overflow-hidden">
      <div className="border-border bg-muted/40 text-muted-foreground flex h-10 shrink-0 items-center border-b px-4 text-[10px] font-semibold tracking-wider uppercase sm:h-12">
        Task definition
      </div>
      <div className="flex flex-1 flex-col overflow-hidden">
        {sideBySideLeft}
      </div>
    </div>
  );

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
            {trialContent}
          </div>
        </ResizablePanel>
      </ResizablePanelGroup>
    ) : taskOnlyActive ? (
      taskFilesPane
    ) : (
      <div className="flex h-full flex-col overflow-hidden">{trialContent}</div>
    );

  // The toolbar lives only in trial mode where there are two panes to manage.
  const showToolbar =
    displayMode === "trial" &&
    (onShowTaskChange || onShowTrialChange) &&
    hasLeft;

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
      {showToolbar && (
        <div className="absolute top-3 right-14 z-20 flex items-center gap-1">
          {onShowTaskChange && (
            <Button
              type="button"
              size="sm"
              variant={showTask ? "default" : "outline"}
              className="h-7 px-2 text-[10px] font-semibold tracking-wide uppercase"
              onClick={handleToggleTask}
              disabled={showTask && !showTrial}
              aria-pressed={showTask}
              title={
                showTask
                  ? trialOnlyActive
                    ? "Show task files"
                    : "Hide task files pane"
                  : "Show task files"
              }
            >
              {showTask && showTrial ? (
                <PanelLeftClose className="mr-1 h-3.5 w-3.5" />
              ) : (
                <Columns2 className="mr-1 h-3.5 w-3.5" />
              )}
              <span className="hidden sm:inline">
                {showTask && showTrial ? "Hide task" : "Show task"}
              </span>
            </Button>
          )}
          {onShowTrialChange && (
            <Button
              type="button"
              size="sm"
              variant={showTrial ? "default" : "outline"}
              className="h-7 px-2 text-[10px] font-semibold tracking-wide uppercase"
              onClick={handleToggleTrial}
              disabled={showTrial && !showTask}
              aria-pressed={showTrial}
              title={
                showTrial
                  ? taskOnlyActive
                    ? "Show trial detail"
                    : "Hide trial detail pane"
                  : "Show trial detail"
              }
            >
              {showTrial && showTask ? (
                <PanelRightClose className="mr-1 h-3.5 w-3.5" />
              ) : (
                <Columns2 className="mr-1 h-3.5 w-3.5" />
              )}
              <span className="hidden sm:inline">
                {showTrial && showTask ? "Hide trial" : "Show trial"}
              </span>
            </Button>
          )}
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
