"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { TASKS_PAGE_SIZE } from "@/lib/tasks-filters";
import { useTaskBrowse } from "@/lib/use-task-browse";
import { cn } from "@/lib/utils";
import { TaskCard } from "./task-card";
import { TasksGridSkeleton } from "./tasks-grid-skeleton";

function pageHref(sp: URLSearchParams, offset: number): string {
  const params = new URLSearchParams(sp.toString());
  if (offset <= 0) params.delete("offset");
  else params.set("offset", String(offset));
  const query = params.toString();
  return query ? `/tasks?${query}` : "/tasks";
}

const pagerClass =
  "inline-flex h-8 items-center gap-1 rounded-md border border-[#6f88b4]/30 px-3 text-[11px] transition-colors";

// The browse fetch happens here, on the client, through useTaskBrowse — the
// document streams without waiting for it, and a return visit paints from
// the SWR cache and revalidates in the background. The URL stays the source
// of truth: sidebar writes and the pager links change searchParams, which
// changes the SWR key.
export function RecentTasksResults() {
  const searchParams = useSearchParams();
  const sp = new URLSearchParams(searchParams.toString());
  const offset = Math.max(Number(sp.get("offset") ?? "0") || 0, 0);
  const { data, error, isLoading, mutate } = useTaskBrowse(sp);

  if (!data) {
    if (error) {
      return (
        <Alert variant="destructive">
          <AlertTitle>Failed to load tasks</AlertTitle>
          <AlertDescription>
            Check the API connection and try again.{" "}
            <button
              type="button"
              onClick={() => void mutate()}
              className="font-medium underline underline-offset-2"
            >
              Retry
            </button>
          </AlertDescription>
        </Alert>
      );
    }
    return <TasksGridSkeleton />;
  }

  const items = data.items ?? [];
  const hasMore = data.has_more ?? false;

  // isLoading while data is present = a different key (filter/pager change)
  // is in flight and keepPreviousData is showing the previous state, dimmed.
  // Background revalidation of the current key never dims.
  if (items.length === 0) {
    return (
      <div
        className={cn(
          "bg-card/60 text-muted-foreground rounded-lg border border-dashed border-[#6f88b4]/30 px-6 py-10 text-center text-sm transition-opacity",
          isLoading && "opacity-60"
        )}
      >
        No tasks match the current filters.
      </div>
    );
  }

  return (
    <div
      className={cn("space-y-4 transition-opacity", isLoading && "opacity-60")}
    >
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {items.map((task) => (
          <TaskCard key={task.id} task={task} />
        ))}
      </div>

      <div className="flex items-center justify-between gap-2">
        <div className="text-muted-foreground text-xs">
          {offset + 1}-{offset + items.length} shown
        </div>
        <div className="flex items-center gap-2">
          {offset === 0 ? (
            <span
              className={cn(
                pagerClass,
                "text-muted-foreground/50 cursor-not-allowed"
              )}
              aria-disabled
            >
              <ChevronLeft className="h-3.5 w-3.5" />
              Previous page
            </span>
          ) : (
            <Link
              href={pageHref(sp, offset - TASKS_PAGE_SIZE)}
              scroll={false}
              className={cn(pagerClass, "hover:bg-muted")}
            >
              <ChevronLeft className="h-3.5 w-3.5" />
              Previous page
            </Link>
          )}
          {hasMore ? (
            <Link
              href={pageHref(sp, offset + TASKS_PAGE_SIZE)}
              scroll={false}
              className={cn(pagerClass, "hover:bg-muted")}
            >
              Next page
              <ChevronRight className="h-3.5 w-3.5" />
            </Link>
          ) : (
            <span
              className={cn(
                pagerClass,
                "text-muted-foreground/50 cursor-not-allowed"
              )}
              aria-disabled
            >
              Next page
              <ChevronRight className="h-3.5 w-3.5" />
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
