"use client";

import { useEffect, useRef } from "react";
import type { TrialDrawerLayout } from "@/lib/user-ui-layout";
import type { ImperativePanelGroupHandle } from "react-resizable-panels";
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
  layout: TrialDrawerLayout;
  onLayoutChange: (patch: Partial<TrialDrawerLayout>) => void;
  onLayoutCommit: () => void;
  layoutSaveError?: boolean;
  onRetryLayoutSave?: () => void;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  mode: DrawerMode;
  taskContent: React.ReactNode;
  renderTrial?: (paneAction: React.ReactNode) => React.ReactNode;
  trialContent?: React.ReactNode;
  showTask?: boolean;
  showTrial?: boolean;
  onShowTaskChange?: (next: boolean) => void;
  onShowTrialChange?: (next: boolean) => void;
  sideBySideLeft?: React.ReactNode;
  defaultWidth?: number;
  sideBySideWidth?: number;
  minWidth?: number;
  maxWidth?: number;
}

export function UnifiedDrawerWrapper({
  layout,
  onLayoutChange,
  onLayoutCommit,
  layoutSaveError,
  onRetryLayoutSave,
  open,
  onOpenChange,
  mode,
  taskContent,
  renderTrial,
  trialContent,
  showTask = layout.showTask,
  showTrial = layout.showTrial,
  onShowTaskChange,
  onShowTrialChange,
  sideBySideLeft,
  defaultWidth = 1080,
  sideBySideWidth = 1500,
  minWidth = 420,
  maxWidth = 1800,
}: UnifiedDrawerWrapperProps) {
  const hasLeft = Boolean(sideBySideLeft);
  const sideBySideActive = mode === "trial" && showTask && showTrial && hasLeft;
  const taskOnlyActive = mode === "trial" && showTask && hasLeft && !showTrial;

  const width =
    layout.preferredWidthPx ??
    (sideBySideActive ? sideBySideWidth : defaultWidth);

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
      <div
        className={cn(
          "border-border bg-muted/40 flex h-10 shrink-0 items-center justify-between gap-2 border-b px-2 sm:h-12 sm:px-3",
          taskOnlyActive && "pr-24 sm:pr-24"
        )}
      >
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

  const showLeftPane = mode === "trial" && hasLeft && showTask;
  const showTrialPane = mode === "trial" && !taskOnlyActive;

  const panelGroupRef = useRef<ImperativePanelGroupHandle>(null);
  const bothPanesShown = showLeftPane && showTrialPane;
  // Apply server restoration and re-expand to the last useful ratio after a
  // Hide/Show toggle. Programmatic layout changes never write preferences.
  useEffect(() => {
    if (!open || !bothPanesShown) return;
    panelGroupRef.current?.setLayout([
      layout.taskPanePercent,
      100 - layout.taskPanePercent,
    ]);
  }, [open, bothPanesShown, layout.taskPanePercent]);

  const commitSplit = () => {
    const sizes = panelGroupRef.current?.getLayout();
    // A drag collapse is transient; visibility has its own explicit controls.
    if (sizes?.length === 2 && sizes.every((size) => size >= 15)) {
      onLayoutChange({ taskPanePercent: sizes[0] });
      onLayoutCommit();
    }
  };

  const body =
    mode === "task" ? (
      <div className="flex h-full flex-col overflow-hidden">{taskContent}</div>
    ) : (
      <ResizablePanelGroup
        ref={panelGroupRef}
        direction="horizontal"
        className="h-full"
      >
        {showLeftPane ? (
          <ResizablePanel
            key="task-pane"
            id="task-pane"
            order={1}
            defaultSize={layout.taskPanePercent}
            // Collapsible so the divider drags all the way over and one pane
            // takes the whole drawer. Recoverable by dragging the handle back
            // out, or via the Hide/Show toggle in the *other* pane's header.
            minSize={15}
            collapsible
            collapsedSize={0}
          >
            {taskFilesPane}
          </ResizablePanel>
        ) : null}
        {sideBySideActive ? (
          <ResizableHandle
            key="pane-handle"
            withHandle
            onDragging={(dragging) => {
              if (!dragging) commitSplit();
            }}
            onKeyUp={commitSplit}
          />
        ) : null}
        {showTrialPane ? (
          <ResizablePanel
            key="trial-pane"
            id="trial-pane"
            order={2}
            defaultSize={100 - layout.taskPanePercent}
            minSize={15}
            collapsible
            collapsedSize={0}
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
      onWidthChange={(preferredWidthPx) => onLayoutChange({ preferredWidthPx })}
      maximized={layout.maximized}
      onMaximizedChange={(maximized) => onLayoutChange({ maximized })}
      onResizeEnd={onLayoutCommit}
    >
      {layoutSaveError && (
        <div
          role="status"
          className="bg-muted flex shrink-0 items-center gap-2 px-3 py-2 pr-24 text-xs"
        >
          Layout preferences could not sync.
          <Button variant="ghost" size="sm" onClick={onRetryLayoutSave}>
            Retry
          </Button>
        </div>
      )}
      <div className="flex flex-1 flex-col overflow-hidden">{body}</div>
    </ResizableDrawer>
  );
}
