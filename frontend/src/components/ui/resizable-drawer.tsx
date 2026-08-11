"use client";

import * as React from "react";
import { X, GripVertical, Maximize2, Minimize2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

interface ResizableDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: React.ReactNode;
  defaultWidth?: number;
  minWidth?: number;
  /**
   * Upper bound for the drag before the viewport is known (on the server and
   * the first paint). Once mounted the viewport is the ceiling instead, so the
   * drawer can always be pulled out to the whole screen.
   */
  maxWidth?: number;
  className?: string;
  /** Hide the close button (useful when parent controls closing) */
  hideCloseButton?: boolean;
  /** Keep drawer mounted in DOM when closed (for smoother transitions) */
  keepMounted?: boolean;
  /** Controlled width — when set, the drawer uses this width instead of internal state. */
  width?: number;
  /** Called whenever the user drags the resize handle. */
  onWidthChange?: (width: number) => void;
}

/** Viewport width, or null until mounted (there is no window on the server). */
function useViewportWidth(): number | null {
  const [viewportWidth, setViewportWidth] = React.useState<number | null>(null);
  React.useEffect(() => {
    const read = () => setViewportWidth(window.innerWidth);
    read();
    window.addEventListener("resize", read);
    return () => window.removeEventListener("resize", read);
  }, []);
  return viewportWidth;
}

export function ResizableDrawer({
  open,
  onOpenChange,
  children,
  defaultWidth = 600,
  minWidth = 300,
  maxWidth = 1200,
  className,
  hideCloseButton = false,
  width: controlledWidth,
  onWidthChange,
}: ResizableDrawerProps) {
  const [internalWidth, setInternalWidth] = React.useState(defaultWidth);
  const width = controlledWidth ?? internalWidth;
  const setWidth = React.useCallback(
    (next: number) => {
      if (controlledWidth === undefined) {
        setInternalWidth(next);
      }
      onWidthChange?.(next);
    },
    [controlledWidth, onWidthChange],
  );
  const [isResizing, setIsResizing] = React.useState(false);
  const drawerRef = React.useRef<HTMLDivElement>(null);

  const viewportWidth = useViewportWidth();
  const widthCeiling = viewportWidth ?? maxWidth;
  // Its own state rather than a `width === ceiling` derivation, so the drawer
  // keeps filling the screen when the window is resized and so `width` survives
  // as the restore target.
  const [maximized, setMaximized] = React.useState(false);
  const displayWidth = maximized
    ? widthCeiling
    : Math.max(Math.min(width, widthCeiling), Math.min(minWidth, widthCeiling));

  // Handle resize via mouse drag
  const handleMouseDown = React.useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      setIsResizing(true);

      const startX = e.clientX;
      const startWidth = displayWidth;

      const handleMouseMove = (moveEvent: MouseEvent) => {
        const deltaX = startX - moveEvent.clientX;
        const newWidth = Math.min(
          widthCeiling,
          Math.max(minWidth, startWidth + deltaX),
        );
        setMaximized(newWidth >= widthCeiling);
        setWidth(newWidth);
      };

      const handleMouseUp = () => {
        setIsResizing(false);
        document.removeEventListener("mousemove", handleMouseMove);
        document.removeEventListener("mouseup", handleMouseUp);
      };

      document.addEventListener("mousemove", handleMouseMove);
      document.addEventListener("mouseup", handleMouseUp);
    },
    [displayWidth, minWidth, widthCeiling, setWidth],
  );

  const toggleMaximized = React.useCallback(() => setMaximized((m) => !m), []);

  // Handle escape key to close
  React.useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && open) {
        onOpenChange(false);
      }
    };

    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, onOpenChange]);

  if (!open) return null;

  return (
    <>
      {/* Backdrop overlay - click to close */}
      <div
        className="animate-in fade-in fixed inset-0 z-30 bg-black/20 duration-300"
        style={{ top: "calc(56px + var(--preview-banner-h, 0px))" }}
        onClick={() => onOpenChange(false)}
      />

      {/* Drawer */}
      <div
        ref={drawerRef}
        className={cn(
          "border-border bg-background fixed right-0 z-40 flex border-l shadow-2xl",
          "animate-in slide-in-from-right duration-300",
          isResizing && "select-none",
          "border-t",
          // At full width the left border sits off-screen and the rounded
          // corner would clip content against the viewport edge.
          maximized ? "border-l-0" : "rounded-tl-lg",
          className,
        )}
        style={{
          width: `${displayWidth}px`,
          // Below the nav header (h-14 = 56px) + optional preview banner
          top: "calc(56px + var(--preview-banner-h, 0px))",
          height: "calc(100vh - 56px - var(--preview-banner-h, 0px))",
        }}
        onClick={(e) => e.stopPropagation()} // Prevent closing when clicking inside drawer
      >
        {/* Resize handle */}
        <div
          className="group hover:bg-primary/20 active:bg-primary/30 absolute top-0 bottom-0 left-0 flex w-1 cursor-ew-resize items-center justify-center"
          onMouseDown={handleMouseDown}
        >
          <div className="bg-muted absolute left-0 flex h-12 w-4 -translate-x-1/2 items-center justify-center rounded-l border border-r-0 opacity-0 transition-opacity group-hover:opacity-100">
            <GripVertical className="text-muted-foreground h-4 w-4" />
          </div>
        </div>

        {/* Top right buttons */}
        <div className="absolute top-4 right-4 z-10 flex items-center gap-1">
          {/* Maximize / restore button */}
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={toggleMaximized}
            aria-pressed={maximized}
            title={maximized ? "Restore width" : "Expand to full screen"}
            className="h-8 w-8 opacity-70 hover:opacity-100"
          >
            {maximized ? (
              <Minimize2 className="h-4 w-4" />
            ) : (
              <Maximize2 className="h-4 w-4" />
            )}
            <span className="sr-only">
              {maximized ? "Restore width" : "Expand to full screen"}
            </span>
          </Button>

          {/* Close button */}
          {!hideCloseButton && (
            <Button
              type="button"
              variant="ghost"
              size="icon"
              onClick={() => onOpenChange(false)}
              className="h-8 w-8 opacity-70 hover:opacity-100"
            >
              <X className="h-4 w-4" />
              <span className="sr-only">Close</span>
            </Button>
          )}
        </div>

        {/* Content */}
        <div className="flex flex-1 flex-col overflow-hidden">{children}</div>
      </div>
    </>
  );
}

// Sub-components for consistent structure
export function DrawerHeader({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("flex flex-col space-y-2 text-left", className)}
      {...props}
    />
  );
}

export function DrawerTitle({
  className,
  ...props
}: React.HTMLAttributes<HTMLHeadingElement>) {
  return (
    <h2
      className={cn("text-foreground text-lg font-semibold", className)}
      {...props}
    />
  );
}

export function DrawerDescription({
  className,
  ...props
}: React.HTMLAttributes<HTMLParagraphElement>) {
  return (
    <p
      className={cn("text-muted-foreground sr-only text-sm", className)}
      {...props}
    />
  );
}
