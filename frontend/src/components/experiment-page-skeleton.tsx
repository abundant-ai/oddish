import { Skeleton } from "@/components/ui/skeleton";
import { TrialGridSkeleton } from "@/components/trial-grid-skeleton";

const SKELETON_COLUMN_COUNT = 6;
const SKELETON_ROW_COUNT = 12;

export function ExperimentPageSkeleton() {
  return (
    <div className="space-y-4" aria-busy="true" aria-label="Loading experiment">
      <div className="space-y-2">
        <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <Skeleton className="h-10 w-[340px]" />
            <Skeleton className="h-4 w-64" />
          </div>
          <div className="flex items-center gap-2">
            <Skeleton className="h-8 w-24" />
            <Skeleton className="h-8 w-20" />
            <Skeleton className="h-8 w-24" />
          </div>
        </div>
        <Skeleton className="h-4 w-40" />
      </div>

      <Skeleton className="h-12 w-full rounded-[10px]" />

      <div className="space-y-3">
        <div className="flex items-center justify-end">
          <Skeleton className="h-8 w-28" />
        </div>
        <div className="border-border bg-card max-w-full overflow-hidden rounded-lg border shadow-xs">
          <div className="border-border bg-card/70 space-y-3 border-b px-3 py-3">
            <div className="flex flex-wrap items-start gap-3">
              <Skeleton className="h-9 w-full sm:w-[320px]" />
              <div className="ml-auto flex flex-wrap items-center gap-2">
                {Array.from({ length: 5 }).map((_, index) => (
                  <Skeleton key={index} className="h-6 w-24" />
                ))}
              </div>
            </div>
          </div>
          <TrialGridSkeleton
            columnCount={SKELETON_COLUMN_COUNT}
            rowCount={SKELETON_ROW_COUNT}
          />
        </div>
      </div>
    </div>
  );
}
