import { auth } from "@clerk/nextjs/server";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { TooltipProvider } from "@/components/ui/tooltip";
import { TasksPageNumber, TasksToolbar } from "./tasks-client";
import { TasksFilterSidebar } from "./tasks-filter-sidebar";
import { SelectionProvider } from "./selection-context";
import { RecentTasksResults } from "./recent-tasks-results";

// Reads auth, so the route is always dynamically rendered. Forcing it also
// avoids the static-prerender error for the client components that call
// useSearchParams (sidebar, toolbar, results grid).
export const dynamic = "force-dynamic";

export default async function TasksPage() {
  const { orgId } = await auth();

  return (
    <SelectionProvider>
      <TooltipProvider>
        <div className="space-y-6">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-start">
            <TasksFilterSidebar />
            <div className="min-w-0 flex-1">
              <Card className="border-[#6f88b4]/20 shadow-xs">
                <CardHeader className="flex flex-col gap-3 pb-3 sm:flex-row sm:items-center sm:justify-between">
                  <div className="space-y-1">
                    <CardTitle className="text-base">Recent Tasks</CardTitle>
                    <div className="text-muted-foreground text-[11px]">
                      <TasksPageNumber />
                    </div>
                  </div>
                  <TasksToolbar orgId={orgId ?? null} />
                </CardHeader>
                <CardContent className="space-y-4">
                  <RecentTasksResults />
                </CardContent>
              </Card>
            </div>
          </div>
        </div>
      </TooltipProvider>
    </SelectionProvider>
  );
}
