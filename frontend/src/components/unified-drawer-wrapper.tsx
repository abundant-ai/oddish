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

type DrawerMode = "task" | "trial";

interface UnifiedDrawerWrapperProps {
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
  const hasLeft = Boolean(sideBySideLeft);
  const sideBySideActive = mode === "trial" && showTask && showTrial && hasLeft;
  const taskOnlyActive =
    mode === "trial" && showTask && hasLeft && !showTrial;

  const [width, setWidth] = useState(
    sideBySideActive ? sideBySideWidth : defaultWidth,
  );
  const userResizedRef = useRef(false);

  useEffect(() => {
    if (userResizedRef.current) return;
    setWidth(sideBySideActive ? sideBySideWidth : defaultWidth);
  }, [sideBySideActive, sideBySideWidth, defaultWidth]);

  const handleWidthChange = (next: number) => {
    userResizedRef.current = true;
    setWidth(next);
  };

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

  const showLeftPane = mode === "trial" && hasLeft && showTask;
  const showTrialPane = mode === "trial" && !taskOnlyActive;

  const body =
    mode === "task" ? (
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
      <div className="flex flex-1 flex-col overflow-hidden">{body}</div>
    </ResizableDrawer>
  );
}
