"use client";

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { Button } from "@/components/ui/button";
import { CostWithBreakdown } from "@/components/cost-with-breakdown";
import type { TaskBrowseItem } from "@/lib/types";

type Entry = {
  name: string;
  inference: number;
  qa: number;
  compute: number;
  estimated: boolean;
};

type SelectionContextValue = {
  isSelected: (id: string) => boolean;
  toggle: (task: TaskBrowseItem) => void;
};

const SelectionContext = createContext<SelectionContextValue | null>(null);

export function useSelection(): SelectionContextValue {
  const ctx = useContext(SelectionContext);
  if (!ctx) {
    throw new Error("useSelection must be used within a SelectionProvider");
  }
  return ctx;
}

// Holds the cost multi-select across pages. Cards are server-rendered, so the
// per-card checkbox state lives here in a client context rather than in the
// (now server) results component.
export function SelectionProvider({ children }: { children: ReactNode }) {
  const [selected, setSelected] = useState<Map<string, Entry>>(new Map());

  const toggle = useCallback((task: TaskBrowseItem) => {
    setSelected((prev) => {
      const next = new Map(prev);
      if (next.has(task.id)) {
        next.delete(task.id);
      } else {
        next.set(task.id, {
          name: task.name,
          // Composite components (inference + QA + compute), matching the dollar
          // the card headlines. The selection "Total" sums the composite (not
          // inference alone) and the footer shows the same ⓘ breakdown.
          inference: task.cost_usd ?? 0,
          qa: task.qa_cost_usd ?? 0,
          compute: task.compute_cost_usd ?? 0,
          estimated: task.cost_has_estimated && !task.cost_has_native,
        });
      }
      return next;
    });
  }, []);

  const total = useMemo(() => {
    let inference = 0;
    let qa = 0;
    let compute = 0;
    let anyEstimated = false;
    for (const entry of selected.values()) {
      inference += entry.inference;
      qa += entry.qa;
      compute += entry.compute;
      if (entry.estimated) anyEstimated = true;
    }
    return {
      count: selected.size,
      inference,
      qa,
      compute,
      cost: inference + qa + compute,
      anyEstimated,
    };
  }, [selected]);

  const value = useMemo<SelectionContextValue>(
    () => ({ isSelected: (id) => selected.has(id), toggle }),
    [selected, toggle],
  );

  return (
    <SelectionContext.Provider value={value}>
      {children}
      {total.count > 0 ? (
        <div className="bg-card/95 sticky bottom-4 z-20 mx-auto flex w-fit max-w-[95%] items-center gap-4 rounded-full border border-[#6f88b4]/40 px-5 py-2.5 shadow-lg backdrop-blur">
          <span className="text-sm font-medium">
            {total.count} task{total.count === 1 ? "" : "s"} selected
          </span>
          <span className="text-sm">
            <span className="text-muted-foreground">Total: </span>
            <CostWithBreakdown
              total={total.cost}
              inference={total.inference}
              qa={total.qa}
              compute={total.compute}
              estimated={total.anyEstimated}
              className="font-semibold tabular-nums"
            />
          </span>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-7 px-2 text-[11px]"
            onClick={() => setSelected(new Map())}
          >
            Clear
          </Button>
        </div>
      ) : null}
    </SelectionContext.Provider>
  );
}
