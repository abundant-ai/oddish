import { Skeleton } from "@/components/ui/skeleton";

// Placeholder grid shown while the server fetches the (filtered) task page.
// Mirrors the TaskCard layout: header row, trial squares, two stat boxes.
export function TasksGridSkeleton({ count = 9 }: { count?: number }) {
  return (
    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
      {Array.from({ length: count }).map((_, index) => (
        <div
          key={index}
          className="bg-card/95 rounded-lg border border-[#6f88b4]/20 px-5 py-5 shadow-xs"
        >
          <div className="flex items-start justify-between gap-3">
            <Skeleton className="mt-0.5 h-4 w-4 shrink-0 rounded" />
            <div className="min-w-0 flex-1 space-y-2">
              <Skeleton className="h-5 w-40 max-w-full" />
              <Skeleton className="h-4 w-12" />
            </div>
            <div className="flex shrink-0 flex-col items-end gap-2">
              <Skeleton className="h-3 w-16" />
              <Skeleton className="h-3 w-20" />
              <Skeleton className="h-4 w-14" />
            </div>
          </div>
          <div className="mt-4 space-y-1.5">
            <Skeleton className="h-3 w-20" />
            <div className="flex flex-wrap gap-1">
              {Array.from({ length: 7 }).map((__, i) => (
                <Skeleton key={i} className="h-[18px] w-[18px] rounded-[4px]" />
              ))}
            </div>
          </div>
          <div className="mt-4 grid gap-2.5 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.45fr)]">
            <Skeleton className="h-16 w-full rounded-md" />
            <Skeleton className="h-16 w-full rounded-md" />
          </div>
        </div>
      ))}
    </div>
  );
}
