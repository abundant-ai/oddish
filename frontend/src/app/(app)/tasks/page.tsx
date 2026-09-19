import { TooltipProvider } from "@/components/ui/tooltip";
import { TasksMatchCount, TasksHeader } from "./tasks-client";
import { TasksFilters } from "./tasks-filters";
import {
  SelectionBar,
  SelectionProvider,
  TasksSelectionControls,
} from "./selection-context";
import { RecentTasksResults } from "./recent-tasks-results";

export const dynamic = "force-dynamic";

export default function TasksPage() {
  return (
    <SelectionProvider>
      <TooltipProvider>
        <div className="space-y-5" data-testid="tasks-browser">
          <TasksHeader />
          <TasksFilters />
          <div className="flex flex-wrap items-center justify-between gap-3">
            <span className="text-muted-foreground text-sm">
              <TasksMatchCount />
            </span>
            <TasksSelectionControls />
          </div>
          <RecentTasksResults />
          <div className="bg-background/95 sticky bottom-3 z-10 rounded-lg">
            <SelectionBar />
          </div>
        </div>
      </TooltipProvider>
    </SelectionProvider>
  );
}
