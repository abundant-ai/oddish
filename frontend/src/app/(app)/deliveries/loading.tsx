import { Skeleton } from "@/components/ui/skeleton";

export default function DeliveriesLoading() {
  return (
    <div className="space-y-6" role="status" aria-label="Loading deliveries">
      <span className="sr-only">Loading deliveries…</span>
      <div
        className="flex items-center justify-between gap-4"
        aria-hidden="true"
      >
        <div className="space-y-2">
          <Skeleton className="h-7 w-48" />
          <Skeleton className="h-4 w-64 max-w-full" />
        </div>
        <Skeleton className="h-9 w-28" />
      </div>
      <div className="space-y-4 rounded-lg border p-4" aria-hidden="true">
        <Skeleton className="h-9 w-64 max-w-full" />
        {Array.from({ length: 8 }, (_, index) => (
          <Skeleton key={index} className="h-12 w-full" />
        ))}
      </div>
    </div>
  );
}
