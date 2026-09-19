"use client";

import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { clearTaskFilters, updateTaskSearchParams } from "@/lib/tasks-filters";
import { TooltipProvider } from "@/components/ui/tooltip";
import { TasksMatchCount, TasksHeader } from "./tasks-client";
import { TasksFilters } from "./tasks-filters";
import {
  SelectionBar,
  SelectionProvider,
  TasksSelectionControls,
} from "./selection-context";
import { RecentTasksResults } from "./recent-tasks-results";

export const dynamic = "force-dynamic";

export default function TasksPage() {
  const searchParams = useSearchParams();
  // Free-text search lives in the URL `q` param (debounced). `query` is the
  // legacy param some deep links still use — read it as a fallback.
  const urlSearch = searchParams.get("q") ?? searchParams.get("query") ?? "";
  const [searchQuery, setSearchQuery] = useState(urlSearch);

  // Search values committed below whose navigations haven't landed yet. Lets
  // the re-sync effect tell "our own commit landing" (skip — the input may
  // already be ahead of it) from an external URL change.
  const pendingSearchCommits = useRef<string[]>([]);

  const searchTimer = useRef<number | null>(null);

  // Re-sync the input when the URL search text changes externally (back/forward,
  // applying a saved filter, Clear all) — but never for our own commits landing,
  // which would clobber whatever the user has typed since.
  useEffect(() => {
    const pending = pendingSearchCommits.current;
    const landed = pending.indexOf(urlSearch);
    if (landed !== -1) {
      pending.splice(0, landed + 1);
      return;
    }
    pendingSearchCommits.current = [];
    if (searchTimer.current !== null) window.clearTimeout(searchTimer.current);
    setSearchQuery((prev) => (prev.trim() === urlSearch ? prev : urlSearch));
  }, [urlSearch]);

  useEffect(() => {
    const handle = window.setTimeout(() => {
      updateTaskSearchParams((params) => {
        const trimmed = searchQuery.trim();
        // Already committed (e.g. a whitespace-only edit) — skip the refetch.
        if (trimmed === (params.get("q") ?? params.get("query") ?? "")) return;
        if (trimmed) params.set("q", trimmed);
        else params.delete("q");
        params.delete("query"); // collapse the legacy param into `q`
        pendingSearchCommits.current.push(trimmed);
      });
    }, 300);
    searchTimer.current = handle;
    return () => window.clearTimeout(handle);
  }, [searchQuery]);

  const [addedKeys, setAddedKeys] = useState<string[]>([]);

  const clearFilters = () => {
    if (searchTimer.current !== null) window.clearTimeout(searchTimer.current);
    pendingSearchCommits.current = [];
    setSearchQuery("");
    setAddedKeys([]);
    clearTaskFilters();
  };

  return (
    <SelectionProvider>
      <TooltipProvider>
        <div className="space-y-5" data-testid="tasks-browser">
          <TasksHeader />
          <TasksFilters
            searchQuery={searchQuery}
            onSearchChange={setSearchQuery}
            addedKeys={addedKeys}
            setAddedKeys={setAddedKeys}
            onClearFilters={clearFilters}
          />
          <div className="flex flex-wrap items-center justify-between gap-3">
            <span className="text-muted-foreground text-sm">
              <TasksMatchCount />
            </span>
            <TasksSelectionControls />
          </div>
          <RecentTasksResults onClearFilters={clearFilters} />
          <div className="bg-background/95 sticky bottom-3 z-10 rounded-lg">
            <SelectionBar />
          </div>
        </div>
      </TooltipProvider>
    </SelectionProvider>
  );
}
