"use client";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { formatCostUsd } from "@/lib/format";
import {
  type SpendGroup,
  type SpendOptionId,
  groupSelectionState,
  setGroupSelection,
  summarizeSpendSelection,
  toggleSpendOption,
} from "@/lib/spend-filter";
import { ChevronDown } from "lucide-react";

type SpendSourceFilterProps = {
  groups: SpendGroup[];
  selected: ReadonlySet<SpendOptionId>;
  onChange: (next: Set<SpendOptionId>) => void;
};

function GroupHeaderCheckbox({
  group,
  selected,
  onChange,
}: {
  group: SpendGroup;
  selected: ReadonlySet<SpendOptionId>;
  onChange: (next: Set<SpendOptionId>) => void;
}) {
  const state = groupSelectionState(group, selected);
  const checked = state === true;
  const indeterminate = state === "indeterminate";

  return (
    <label className="hover:bg-accent/50 flex cursor-pointer items-start gap-2 rounded-sm px-1 py-1">
      <Checkbox
        className="mt-0.5"
        checked={indeterminate ? "indeterminate" : checked}
        disabled={group.options.length === 0}
        onCheckedChange={(value) => {
          onChange(setGroupSelection(selected, group, value === true));
        }}
        aria-label={`Select all ${group.label}`}
      />
      <span className="min-w-0 flex-1">
        <span className="block text-xs font-medium">{group.label}</span>
        <span className="text-muted-foreground block text-[10px] leading-snug">
          {group.hint}
        </span>
      </span>
    </label>
  );
}

export function SpendSourceFilter({
  groups,
  selected,
  onChange,
}: SpendSourceFilterProps) {
  const summary = summarizeSpendSelection(groups, selected);
  const selectedCount = selected.size;

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-8 max-w-[280px] justify-between gap-2 px-2 text-xs"
          aria-label="Filter spend sources"
        >
          <span className="truncate">{summary}</span>
          <span className="text-muted-foreground inline-flex items-center gap-1">
            {selectedCount > 0 ? (
              <span className="tabular-nums">{selectedCount}</span>
            ) : null}
            <ChevronDown className="h-3.5 w-3.5 opacity-50" />
          </span>
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-[320px] p-2">
        <div className="mb-1 px-1 text-[11px] font-medium">Show on chart</div>
        <div className="max-h-[360px] space-y-3 overflow-y-auto pr-1">
          {groups.map((group) => (
            <div key={group.id} className="space-y-1">
              <GroupHeaderCheckbox
                group={group}
                selected={selected}
                onChange={onChange}
              />
              {group.options.length === 0 ? (
                <p className="text-muted-foreground px-7 py-1 text-[11px]">
                  {group.id === "models"
                    ? "No model spend in this window."
                    : "No compute providers yet."}
                </p>
              ) : (
                <ul className="space-y-0.5 pl-6">
                  {group.options.map((option) => {
                    const isChecked = selected.has(option.id);
                    return (
                      <li key={option.id}>
                        <label className="hover:bg-accent/50 flex cursor-pointer items-center gap-2 rounded-sm px-1 py-1">
                          <Checkbox
                            checked={isChecked}
                            onCheckedChange={() =>
                              onChange(toggleSpendOption(selected, option.id))
                            }
                            aria-label={option.label}
                          />
                          <span className="min-w-0 flex-1 truncate text-xs">
                            {option.label}
                          </span>
                          <span
                            className={`shrink-0 font-mono text-[10px] tabular-nums ${
                              option.costUsd > 0
                                ? ""
                                : "text-muted-foreground"
                            }`}
                          >
                            {option.costUsd > 0
                              ? formatCostUsd(option.costUsd)
                              : "—"}
                          </span>
                        </label>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          ))}
        </div>
        <div className="mt-2 flex items-center justify-between gap-2 border-t px-1 pt-2">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-7 px-2 text-[11px]"
            onClick={() => {
              const next = new Set<SpendOptionId>();
              for (const group of groups) {
                for (const option of group.options) next.add(option.id);
              }
              onChange(next);
            }}
          >
            Select all
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-7 px-2 text-[11px]"
            onClick={() => onChange(new Set())}
          >
            Clear
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}
