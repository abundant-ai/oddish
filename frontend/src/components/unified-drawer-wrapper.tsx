"use client";

import { useState, useEffect, useRef } from "react";
import { ResizableDrawer } from "@/components/ui/resizable-drawer";

type DrawerMode = "task" | "trial";

interface UnifiedDrawerWrapperProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  mode: DrawerMode;
  taskContent: React.ReactNode;
  trialContent: React.ReactNode;
  defaultWidth?: number;
  minWidth?: number;
  maxWidth?: number;
}

export function UnifiedDrawerWrapper({
  open,
  onOpenChange,
  mode,
  taskContent,
  trialContent,
  defaultWidth = 820,
  minWidth = 420,
  maxWidth = 1200,
}: UnifiedDrawerWrapperProps) {
  const [displayMode, setDisplayMode] = useState<DrawerMode>(mode);
  const [isTransitioning, setIsTransitioning] = useState(false);
  const previousMode = useRef<DrawerMode>(mode);

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

  return (
    <ResizableDrawer
      open={open}
      onOpenChange={onOpenChange}
      defaultWidth={defaultWidth}
      minWidth={minWidth}
      maxWidth={maxWidth}
    >
      <div
        className="h-full transition-opacity duration-300"
        style={{
          opacity: isTransitioning ? 0.3 : 1,
        }}
      >
        {displayMode === "task" ? taskContent : trialContent}
      </div>
    </ResizableDrawer>
  );
}
