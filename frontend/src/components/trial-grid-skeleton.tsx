import { Skeleton } from "@/components/ui/skeleton";

export function TrialGridSkeleton({
  columnCount,
  rowCount,
}: {
  columnCount: number;
  rowCount: number;
}) {
  const gridTemplateColumns = `240px repeat(${columnCount}, minmax(0, 1fr))`;
  return (
    <div className="overflow-x-auto p-3">
      <div className="w-full min-w-[960px] space-y-2">
        <div
          className="bg-muted/40 grid gap-2 rounded-md p-2"
          style={{ gridTemplateColumns }}
        >
          <Skeleton className="h-5 w-24" />
          {Array.from({ length: columnCount }).map((_, index) => (
            <Skeleton key={index} className="h-5 w-full" />
          ))}
        </div>
        {Array.from({ length: rowCount }).map((_, rowIndex) => (
          <div
            key={rowIndex}
            className="border-border/60 grid gap-2 rounded-md border p-2"
            style={{ gridTemplateColumns }}
          >
            <div className="flex items-center gap-2">
              <Skeleton className="h-4 w-4 rounded-sm" />
              <Skeleton className="h-4 w-40" />
            </div>
            {Array.from({ length: columnCount }).map((_, columnIndex) => (
              <div
                key={columnIndex}
                className="flex items-center justify-center gap-1"
              >
                <Skeleton className="h-5 w-5 rounded-sm" />
                <Skeleton className="h-5 w-5 rounded-sm" />
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
