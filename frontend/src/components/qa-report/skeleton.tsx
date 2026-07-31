import { Skeleton } from "@/components/ui/skeleton";

/** Placeholder with the report card's shape, for while the trial fetch runs. */
export function QaReportSkeleton() {
  return (
    <div className="border-border overflow-hidden rounded-xl border">
      <div className="border-border/60 flex items-center gap-2.5 border-b px-3 py-2.5">
        <Skeleton className="size-4.5 rounded-full" />
        <Skeleton className="h-4 w-32" />
        <Skeleton className="h-4 w-24 rounded-md" />
        <div className="ml-auto flex items-center gap-2">
          <Skeleton className="h-4 w-10" />
          <Skeleton className="h-5 w-20 rounded-md" />
        </div>
      </div>
      <div className="flex flex-col gap-2 px-3 py-3">
        <Skeleton className="h-3 w-full" />
        <Skeleton className="h-3 w-11/12" />
        <Skeleton className="h-3 w-3/5" />
        <Skeleton className="mt-3 h-8 w-full rounded-lg" />
        <Skeleton className="h-3 w-24" />
      </div>
    </div>
  );
}
