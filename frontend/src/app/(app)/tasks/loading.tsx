import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { TasksGridSkeleton } from "./tasks-grid-skeleton";

export default function TasksLoading() {
  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start">
        <aside className="w-full shrink-0 sm:w-56">
          <div className="bg-card/95 space-y-4 rounded-lg border border-[#6f88b4]/20 p-3 shadow-xs">
            <Skeleton className="h-5 w-20" />
            {Array.from({ length: 3 }).map((_, index) => (
              <div key={index} className="space-y-2">
                <Skeleton className="h-3 w-16" />
                <Skeleton className="h-8 w-full" />
              </div>
            ))}
            <Skeleton className="h-8 w-full" />
          </div>
        </aside>
        <div className="min-w-0 flex-1">
          <Card className="border-[#6f88b4]/20 shadow-xs">
            <CardHeader className="flex flex-col gap-3 pb-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="space-y-2">
                <Skeleton className="h-5 w-28" />
                <Skeleton className="h-3 w-16" />
              </div>
              <Skeleton className="h-8 w-64 max-w-full" />
            </CardHeader>
            <CardContent>
              <TasksGridSkeleton />
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}
