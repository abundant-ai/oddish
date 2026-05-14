"use client";

import { useState, useEffect, useRef } from "react";
import { ResizableDrawer } from "@/components/ui/resizable-drawer";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";

type DrawerMode = "task" | "trial";

interface UnifiedDrawerWrapperProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  mode: DrawerMode;
  taskContent: React.ReactNode;
  trialContent: React.ReactNode;
  /** When true and mode==='trial', show task files side-by-side with the trial content. */
  sideBySide?: boolean;
  /** Extra content rendered in the left pane when side-by-side is active (e.g. the task file viewer). */
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
  sideBySideLeft,
  defaultWidth = 1080,
  sideBySideWidth = 1500,
  minWidth = 420,
  maxWidth = 1800,
}: UnifiedDrawerWrapperProps) {
  const [displayMode, setDisplayMode] = useState<DrawerMode>(mode);
  const [isTransitioning, setIsTransitioning] = useState(false);
  const previousMode = useRef<DrawerMode>(mode);

  // Track drawer width so we can grow it when side-by-side is enabled.
  const [width, setWidth] = useState(defaultWidth);
  const userResizedRef = useRef(false);

  // Smooth transition between modes
  useEffect(() => {
    if (mode !== previousMode.current && open) {
      setIsTransitioning(true);

      const timer = setTimeout(() => {
        setDisplayMode(mode);
        setIsTransitioning(false);
        previousMode.current = mode;
      }, 150); // Half of transition duration for crossfade

      return () => clearTimeout(timer);
    } else if (!open) {
      // Reset to current mode when closed
      setDisplayMode(mode);
      previousMode.current = mode;
    }
  }, [mode, open]);

  // Auto-grow / shrink the drawer when side-by-side toggles, unless the user
  // has manually resized it (in which case we leave their width alone).
  const sideBySideActive =
    sideBySide && displayMode === "trial" && Boolean(sideBySideLeft);
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
      taskContent
    ) : sideBySideActive ? (
      <ResizablePanelGroup
        direction="horizontal"
        autoSaveId="trial-detail-side-by-side"
        className="h-full"
      >
        <ResizablePanel defaultSize={40} minSize={20} maxSize={70}>
          <div className="border-border flex h-full flex-col overflow-hidden border-r">
            {sideBySideLeft}
          </div>
        </ResizablePanel>
        <ResizableHandle withHandle />
        <ResizablePanel defaultSize={60} minSize={30}>
          <div className="flex h-full flex-col overflow-hidden">
            {trialContent}
          </div>
        </ResizablePanel>
      </ResizablePanelGroup>
    ) : (
      trialContent
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
        className="h-full transition-opacity duration-300"
        style={{
          opacity: isTransitioning ? 0.3 : 1,
        }}
      >
        {body}
      </div>
    </ResizableDrawer>
  );
}
