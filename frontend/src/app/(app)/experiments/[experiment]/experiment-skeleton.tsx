import { Skeleton } from "@/components/ui/skeleton";

const SKELETON_COLUMN_COUNT = 6;
const SKELETON_ROW_COUNT = 12;

// Height-stable placeholder for the experiment page, used both as the route
// loading.tsx and as the Suspense fallback while the initial task-shells
// promise streams in. Mirrors the real layout (title, meta strip, summary
// bar, toolbar, trial grid) so the swap to live content doesn't shift layout.
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
          <div className="overflow-x-auto p-3">
            <div className="w-full min-w-[960px] space-y-2">
              <div
                className="bg-muted/40 grid gap-2 rounded-md p-2"
                style={{
                  gridTemplateColumns: `240px repeat(${SKELETON_COLUMN_COUNT}, minmax(0, 1fr))`,
                }}
              >
                <Skeleton className="h-5 w-24" />
                {Array.from({ length: SKELETON_COLUMN_COUNT }).map(
                  (_, index) => (
                    <Skeleton key={index} className="h-5 w-full" />
                  ),
                )}
              </div>
              {Array.from({ length: SKELETON_ROW_COUNT }).map((_, rowIndex) => (
                <div
                  key={rowIndex}
                  className="border-border/60 grid gap-2 rounded-md border p-2"
                  style={{
                    gridTemplateColumns: `240px repeat(${SKELETON_COLUMN_COUNT}, minmax(0, 1fr))`,
                  }}
                >
                  <div className="flex items-center gap-2">
                    <Skeleton className="h-4 w-4 rounded-sm" />
                    <Skeleton className="h-4 w-40" />
                  </div>
                  {Array.from({ length: SKELETON_COLUMN_COUNT }).map(
                    (_, columnIndex) => (
                      <div
                        key={columnIndex}
                        className="flex items-center justify-center gap-1"
                      >
                        <Skeleton className="h-5 w-5 rounded-sm" />
                        <Skeleton className="h-5 w-5 rounded-sm" />
                      </div>
                    ),
                  )}
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
