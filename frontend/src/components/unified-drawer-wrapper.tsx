"use client";

import { useState, useEffect, useRef } from "react";
import { Columns2, PanelLeft, PanelRight } from "lucide-react";
import { ResizableDrawer } from "@/components/ui/resizable-drawer";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

type DrawerMode = "task" | "trial";

type PaneLayout = "task" | "split" | "trial";

interface UnifiedDrawerWrapperProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  mode: DrawerMode;
  taskContent: React.ReactNode;
  /**
   * Render the trial detail. Receives a `paneAction` slot that should be
   * placed in the trial header — the pane layout control renders there
   * whenever the task pane is hidden (otherwise it lives in the task
   * pane's own header).
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

const LAYOUT_CHOICES: Array<{
  layout: PaneLayout;
  icon: typeof PanelLeft;
  label: string;
}> = [
  { layout: "task", icon: PanelLeft, label: "Task only" },
  { layout: "split", icon: Columns2, label: "Task and trial side by side" },
  { layout: "trial", icon: PanelRight, label: "Trial only" },
];

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
  const paneLayout: PaneLayout = taskOnlyActive
    ? "task"
    : sideBySideActive
      ? "split"
      : "trial";

  const [width, setWidth] = useState(
    sideBySideActive ? sideBySideWidth : defaultWidth,
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

  const applyLayout = (next: PaneLayout) => {
    onShowTaskChange?.(next !== "trial");
    onShowTrialChange?.(next !== "task");
  };

  // One segmented control for the pane layout (task / split / trial)
  // replaces the old pair of hide/show buttons that lived in different
  // headers with cross-dependent disabled states. It renders at the right
  // of the leftmost visible pane's header: the task pane's when that pane
  // is shown, else the trial header via the paneAction slot.
  const layoutControl =
    hasLeft &&
    displayMode === "trial" &&
    (onShowTaskChange || onShowTrialChange) ? (
      <div
        role="group"
        aria-label="Pane layout"
        className="border-border bg-background/60 flex items-center gap-0.5 rounded-md border p-0.5"
      >
        {LAYOUT_CHOICES.map(({ layout, icon: Icon, label }) => (
          <Button
            key={layout}
            type="button"
            size="icon"
            variant="ghost"
            className={cn(
              "h-6 w-6",
              paneLayout === layout
                ? "bg-muted text-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
            onClick={() => applyLayout(layout)}
            aria-pressed={paneLayout === layout}
            title={label}
          >
            <Icon className="h-3.5 w-3.5" />
          </Button>
        ))}
      </div>
    ) : null;

  const taskFilesPane = (
    <div className="bg-background flex h-full flex-col overflow-hidden">
      <div className="border-border bg-muted/40 flex h-10 shrink-0 items-center justify-between gap-2 border-b px-2 sm:h-12 sm:px-3">
        <span className="text-muted-foreground pl-2 text-[10px] font-semibold tracking-wider uppercase">
          Task definition
        </span>
        {layoutControl}
      </div>
      <div className="flex flex-1 flex-col overflow-hidden">
        {sideBySideLeft}
      </div>
    </div>
  );

  const renderedTrial = renderTrial
    ? renderTrial(paneLayout === "trial" ? layoutControl : null)
    : (trialContent ?? null);

  const showLeftPane = displayMode === "trial" && hasLeft && showTask;
  const showTrialPane = displayMode === "trial" && !taskOnlyActive;

  // The panels carry stable keys plus react-resizable-panels' id/order so
  // toggling one pane never remounts the other — remounting the trial pane
  // used to reset it (tab, file, scroll) every time the task pane was
  // shown or hidden.
  //
  // min/maxSize must stay constant for a panel's whole lifetime: when they
  // change, react-resizable-panels re-clamps the panel against the current
  // layout, and doing that while it's the only mounted panel resolves a
  // pivot index of -1 ("Previous layout not found for panel index -1").
  // A lone panel always occupies 100%, so static minSizes are safe in every
  // layout, and the task pane's 70% cap in split mode is already implied by
  // the trial pane's minSize.
  const body =
    displayMode === "task" ? (
      <div className="flex h-full flex-col overflow-hidden">{taskContent}</div>
    ) : (
      <ResizablePanelGroup
        direction="horizontal"
        autoSaveId="trial-detail-side-by-side"
        className="h-full"
      >
        {showLeftPane ? (
          <ResizablePanel
            key="task-pane"
            id="task-pane"
            order={1}
            defaultSize={42}
            minSize={20}
          >
            {taskFilesPane}
          </ResizablePanel>
        ) : null}
        {sideBySideActive ? (
          <ResizableHandle key="pane-handle" withHandle />
        ) : null}
        {showTrialPane ? (
          <ResizablePanel
            key="trial-pane"
            id="trial-pane"
            order={2}
            defaultSize={58}
            minSize={30}
          >
            <div className="flex h-full flex-col overflow-hidden">
              {renderedTrial}
            </div>
          </ResizablePanel>
        ) : null}
      </ResizablePanelGroup>
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
          "flex flex-1 flex-col overflow-hidden transition-opacity duration-300",
        )}
        style={{ opacity: isTransitioning ? 0.3 : 1 }}
      >
        {body}
      </div>
    </ResizableDrawer>
  );
}
