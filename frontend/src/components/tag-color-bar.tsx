"use client";

import { useState } from "react";

import {
  TAG_COLOR_PALETTE,
  getRecentCustomColors,
  rememberCustomColor,
} from "@/lib/tag-colors";
import { cn } from "@/lib/utils";

interface TagColorBarProps {
  value: string;
  onChange: (color: string) => void;
  className?: string;
}

/**
 * One row of color swatches filling the available width: palette colors up
 * to the last-but-one slot, then a custom color-input slot. Custom picks are
 * remembered and displace the tail of the default palette, so user colors
 * appear in the bar in place of the stock ones.
 */
export function TagColorBar({ value, onChange, className }: TagColorBarProps) {
  const [recents, setRecents] = useState<string[]>(() =>
    getRecentCustomColors(),
  );
  const swatches = [
    ...TAG_COLOR_PALETTE.slice(0, TAG_COLOR_PALETTE.length - recents.length),
    ...recents,
  ];
  const customActive = !swatches.includes(value);

  return (
    <div className={cn("flex w-full items-center justify-between", className)}>
      {swatches.map((swatch) => (
        <button
          key={swatch}
          type="button"
          aria-label={`Tag color ${swatch}`}
          className={cn(
            "h-4 w-4 rounded-full transition-transform hover:scale-110",
            swatch === value &&
              "ring-2 ring-ring ring-offset-1 ring-offset-popover",
          )}
          style={{ backgroundColor: swatch }}
          onClick={() => onChange(swatch)}
        />
      ))}
      <label
        aria-label="Pick a custom color"
        title="Custom color"
        className={cn(
          "relative h-4 w-4 cursor-pointer rounded-full transition-transform hover:scale-110",
          customActive && "ring-2 ring-ring ring-offset-1 ring-offset-popover",
        )}
        style={{
          // Always the rainbow — the slot must stay recognizable as the
          // picker even while a custom color is active (the ring marks
          // that); the picked color itself shows in the preview/recents.
          background:
            "conic-gradient(#ef4444, #f59e0b, #22c55e, #3b82f6, #8b5cf6, #ec4899, #ef4444)",
        }}
      >
        <input
          type="color"
          className="absolute inset-0 h-full w-full cursor-pointer opacity-0"
          value={/^#[0-9a-f]{6}$/i.test(value) ? value : "#22c55e"}
          onChange={(event) => {
            const picked = event.target.value;
            rememberCustomColor(picked);
            setRecents(getRecentCustomColors());
            onChange(picked);
          }}
        />
      </label>
    </div>
  );
}
