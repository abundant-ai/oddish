"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ChevronDown } from "lucide-react";

// Self-contained copy of the Usage tab's time control. Deliberately *not*
// imported from `usage-overview.tsx`: the Cost feature lives on its own branch
// off main, and reusing that component would mean editing a file the
// dashboard/usage branch is also changing (a guaranteed merge conflict). The
// control is small and stable, so a local copy is the cleaner trade.
//
// The one behavioural difference: Cost always resolves to a *bounded* window
// (<= `maxMinutes`, default 60d) so it never issues an unbounded trials scan.
// "All" therefore maps to the cap rather than null.

const TIME_RANGES = [
  { key: "all", label: "All", minutes: null },
  { key: "15m", label: "15m", minutes: 15 },
  { key: "1h", label: "1h", minutes: 60 },
  { key: "24h", label: "24h", minutes: 1440 },
  { key: "7d", label: "7d", minutes: 10080 },
  { key: "30d", label: "30d", minutes: 43200 },
  { key: "60d", label: "60d", minutes: 86400 },
] as const;

type PresetTimeRangeKey = (typeof TIME_RANGES)[number]["key"];
export type TimeRangeKey = PresetTimeRangeKey | `custom:${number}`;

export const COST_MAX_MINUTES = 86400; // 60d

function getRawMinutes(range: TimeRangeKey): number | null {
  if (range.startsWith("custom:")) {
    const value = Number(range.slice("custom:".length));
    return Number.isFinite(value) && value > 0 ? Math.round(value) : null;
  }
  return TIME_RANGES.find((r) => r.key === range)?.minutes ?? null;
}

// Always returns a finite, bounded window. "All" (null) and any custom value
// above the cap collapse to `maxMinutes`.
export function getBoundedMinutes(
  range: TimeRangeKey,
  maxMinutes: number = COST_MAX_MINUTES,
): number {
  const raw = getRawMinutes(range);
  if (raw == null) return maxMinutes;
  return Math.min(raw, maxMinutes);
}

function getTimeRangeLabel(range: TimeRangeKey): string {
  if (!range.startsWith("custom:")) {
    return TIME_RANGES.find((entry) => entry.key === range)?.label ?? "Window";
  }
  const minutes = getRawMinutes(range);
  if (!minutes) return "Custom";
  if (minutes % 1440 === 0) return `${minutes / 1440}d`;
  if (minutes % 60 === 0) return `${minutes / 60}h`;
  return `${minutes}m`;
}

export function TimeRangePicker({
  value,
  onChange,
  maxMinutes = COST_MAX_MINUTES,
}: {
  value: TimeRangeKey;
  onChange: (key: TimeRangeKey) => void;
  maxMinutes?: number;
}) {
  const [isCustomPickerOpen, setIsCustomPickerOpen] = useState(
    value.startsWith("custom:"),
  );
  const [customMagnitude, setCustomMagnitude] = useState("2");
  const [customUnit, setCustomUnit] = useState<"m" | "h" | "d">("h");

  useEffect(() => {
    if (!value.startsWith("custom:")) return;
    const minutes = getRawMinutes(value);
    if (!minutes) return;
    if (minutes % 1440 === 0) {
      setCustomMagnitude(String(minutes / 1440));
      setCustomUnit("d");
      return;
    }
    if (minutes % 60 === 0) {
      setCustomMagnitude(String(minutes / 60));
      setCustomUnit("h");
      return;
    }
    setCustomMagnitude(String(minutes));
    setCustomUnit("m");
  }, [value]);

  const selectedWindowValue = value.startsWith("custom:") ? "custom" : value;
  const selectedWindowLabel = getTimeRangeLabel(value);
  const showCustomControls = isCustomPickerOpen || value.startsWith("custom:");

  const applyCustomWindow = () => {
    const magnitude = Number(customMagnitude);
    if (!Number.isFinite(magnitude) || magnitude <= 0) return;
    const roundedMagnitude = Math.round(magnitude);
    const minutesPerUnit =
      customUnit === "d" ? 1440 : customUnit === "h" ? 60 : 1;
    const minutes = Math.min(
      maxMinutes,
      Math.max(1, roundedMagnitude * minutesPerUnit),
    );
    onChange(`custom:${minutes}`);
    setIsCustomPickerOpen(false);
  };

  return (
    <div className="flex shrink-0 flex-wrap items-center justify-end gap-1">
      <DropdownMenu modal={false}>
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-7 w-[120px] justify-between border-[#6f88b4]/20 px-2 text-[11px]"
            aria-label="Time window"
          >
            <span>{selectedWindowLabel}</span>
            <ChevronDown className="h-3.5 w-3.5 opacity-50" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-[120px]">
          <DropdownMenuRadioGroup
            value={selectedWindowValue}
            onValueChange={(next) => {
              if (next === "custom") {
                setIsCustomPickerOpen(true);
                return;
              }
              setIsCustomPickerOpen(false);
              onChange(next as PresetTimeRangeKey);
            }}
          >
            {TIME_RANGES.map((range) => (
              <DropdownMenuRadioItem
                key={range.key}
                value={range.key}
                className="text-xs"
              >
                {range.label}
              </DropdownMenuRadioItem>
            ))}
            <DropdownMenuRadioItem value="custom" className="text-xs">
              Custom...
            </DropdownMenuRadioItem>
          </DropdownMenuRadioGroup>
        </DropdownMenuContent>
      </DropdownMenu>
      {showCustomControls && (
        <>
          <Input
            value={customMagnitude}
            onChange={(event) => setCustomMagnitude(event.target.value)}
            inputMode="numeric"
            className="h-7 w-[66px] text-[11px]"
            aria-label="Custom time window amount"
          />
          <Select
            value={customUnit}
            onValueChange={(next) => setCustomUnit(next as "m" | "h" | "d")}
          >
            <SelectTrigger
              className="h-7 w-[72px] border-[#6f88b4]/20 text-[11px]"
              aria-label="Custom time window unit"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent align="end">
              <SelectItem value="m" className="text-xs">
                min
              </SelectItem>
              <SelectItem value="h" className="text-xs">
                hour
              </SelectItem>
              <SelectItem value="d" className="text-xs">
                day
              </SelectItem>
            </SelectContent>
          </Select>
          <Button
            variant="secondary"
            size="sm"
            className="h-7 px-2 text-[11px]"
            onClick={applyCustomWindow}
          >
            Apply
          </Button>
        </>
      )}
    </div>
  );
}
