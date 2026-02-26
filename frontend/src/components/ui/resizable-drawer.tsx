"use client";

import * as React from "react";
import { X, GripVertical } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

interface ResizableDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: React.ReactNode;
  defaultWidth?: number;
  minWidth?: number;
  maxWidth?: number;
  className?: string;
  /** Hide the close button (useful when parent controls closing) */
  hideCloseButton?: boolean;
  /** Keep drawer mounted in DOM when closed (for smoother transitions) */
  keepMounted?: boolean;
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
}: ResizableDrawerProps) {
  const [width, setWidth] = React.useState(defaultWidth);
  const [isResizing, setIsResizing] = React.useState(false);
  const drawerRef = React.useRef<HTMLDivElement>(null);

  // Handle resize via mouse drag
  const handleMouseDown = React.useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      setIsResizing(true);

      const startX = e.clientX;
      const startWidth = width;

      const handleMouseMove = (moveEvent: MouseEvent) => {
        const deltaX = startX - moveEvent.clientX;
        const newWidth = Math.min(
          maxWidth,
          Math.max(minWidth, startWidth + deltaX),
        );
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
    [width, minWidth, maxWidth],
  );

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
        className="fixed inset-0 z-30 bg-black/20 animate-in fade-in duration-300"
        style={{ top: "56px" }}
        onClick={() => onOpenChange(false)}
      />

      {/* Drawer */}
      <div
        ref={drawerRef}
        className={cn(
          "fixed right-0 z-40 flex bg-background border-l border-border shadow-2xl",
          "animate-in slide-in-from-right duration-300",
          isResizing && "select-none",
          "border-t rounded-tl-lg",
          className,
        )}
        style={{
          width: `${width}px`,
          top: "56px", // Below the nav header (h-14 = 56px)
          height: "calc(100vh - 56px)",
        }}
        onClick={(e) => e.stopPropagation()} // Prevent closing when clicking inside drawer
      >
        {/* Resize handle */}
        <div
          className="absolute left-0 top-0 bottom-0 w-1 cursor-ew-resize hover:bg-primary/20 active:bg-primary/30 group flex items-center justify-center"
          onMouseDown={handleMouseDown}
        >
          <div className="absolute left-0 w-4 h-12 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity -translate-x-1/2 bg-muted rounded-l border border-r-0">
            <GripVertical className="h-4 w-4 text-muted-foreground" />
          </div>
        </div>

        {/* Top right buttons */}
        <div className="absolute right-4 top-4 z-10 flex items-center gap-1">
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
        <div className="flex-1 flex flex-col overflow-hidden">{children}</div>
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
      className={cn("text-lg font-semibold text-foreground", className)}
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
      className={cn("text-sm text-muted-foreground sr-only", className)}
      {...props}
    />
  );
}
