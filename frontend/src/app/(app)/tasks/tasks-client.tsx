"use client";

import { useEffect, useState, useTransition } from "react";
import { useSearchParams } from "next/navigation";
import { Clock, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ImportDialog } from "@/components/import-dialog";
import { TASKS_PAGE_SIZE } from "@/lib/tasks-filters";
import {
  useTaskBrowseCount,
  useTaskBrowseRevalidate,
} from "@/lib/use-task-browse";
import { cn } from "@/lib/utils";

const AUTO_REFRESH_KEY = "oddish.tasks.autoRefresh";
const REFRESH_MS = 60000;

// The page number derives from the URL on the client, so filter and pager
// changes update it without a server round trip.
export function TasksPageNumber() {
  const searchParams = useSearchParams();
  const offset = Math.max(Number(searchParams.get("offset") ?? "0") || 0, 0);
  return <>Page {Math.floor(offset / TASKS_PAGE_SIZE) + 1}</>;
}

// How many tasks match the active filters across every page — the grid shows
// at most TASKS_PAGE_SIZE of them. Its own request, keyed on the filters
// alone, so it neither delays the cards nor re-runs when you page.
export function TasksMatchCount() {
  const searchParams = useSearchParams();
  const { total, isStale } = useTaskBrowseCount(
    new URLSearchParams(searchParams.toString())
  );
  // Nothing to claim before the first answer lands: a count guessed from the
  // page would be wrong for every filter state with more than one page.
  if (total === null) return null;
  const label = `${total.toLocaleString()} matching ${
    total === 1 ? "task" : "tasks"
  }`;
  // While the next filter state is in flight — or after it failed — the
  // number on screen belongs to the PREVIOUS filters. Dim it and say so,
  // rather than letting a stale total pass as the current answer.
  return (
    <>
      <span
        className={cn(isStale && "opacity-50")}
        aria-busy={isStale || undefined}
        title={isStale ? `${label} (for the previous filters)` : undefined}
      >
        {label}
        {isStale ? "…" : ""}
      </span>
      {" · "}
    </>
  );
}

// Every refresh path revalidates the grid's client-side browse fetch only —
// nothing the page server-renders depends on task data, so there is no
// router.refresh().
export function TasksToolbar() {
  const revalidateBrowse = useTaskBrowseRevalidate();
  const [isPending, startTransition] = useTransition();
  const [autoRefresh, setAutoRefresh] = useState(false);

  // Restore the saved preference client-side (avoids a hydration mismatch).
  useEffect(() => {
    setAutoRefresh(window.localStorage.getItem(AUTO_REFRESH_KEY) === "1");
  }, []);

  const toggleAuto = () => {
    setAutoRefresh((prev) => {
      const next = !prev;
      window.localStorage.setItem(AUTO_REFRESH_KEY, next ? "1" : "0");
      return next;
    });
  };

  // Silent background refresh only while auto-refresh is on.
  useEffect(() => {
    if (!autoRefresh) return;
    const id = window.setInterval(() => {
      void revalidateBrowse();
    }, REFRESH_MS);
    return () => window.clearInterval(id);
  }, [autoRefresh, revalidateBrowse]);

  // The async transition keeps the spinner honest: isPending tracks the
  // browse fetch itself. Failures surface in the grid's error banner.
  const manualRefresh = () =>
    startTransition(async () => {
      await revalidateBrowse();
    });

  return (
    <div className="flex items-center gap-2">
      <Button
        type="button"
        variant="outline"
        size="icon"
        className="h-8 w-8 border-[#6f88b4]/20"
        onClick={manualRefresh}
        disabled={isPending}
        aria-label="Refresh tasks"
        title="Refresh tasks"
      >
        <RefreshCw className={cn("h-4 w-4", isPending && "animate-spin")} />
      </Button>
      <Button
        type="button"
        variant={autoRefresh ? "default" : "outline"}
        size="sm"
        className={cn(
          "h-8 gap-1.5 text-xs",
          !autoRefresh && "border-[#6f88b4]/20"
        )}
        onClick={toggleAuto}
        aria-pressed={autoRefresh}
        title={
          autoRefresh
            ? "Auto-refresh on (every 60s) — click to turn off"
            : "Auto-refresh off — click to refresh every 60s"
        }
      >
        <Clock className="h-3.5 w-3.5" />
        Auto
      </Button>
      <ImportDialog onImported={() => void revalidateBrowse()} />
    </div>
  );
}
