import { Suspense } from "react";
import { auth } from "@clerk/nextjs/server";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { TooltipProvider } from "@/components/ui/tooltip";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import { TASKS_PAGE_SIZE } from "@/lib/tasks-filters";
import type { TaskBrowseFacets } from "@/lib/types";
import { TasksToolbar } from "./tasks-client";
import { TasksFilterSidebar } from "./tasks-filter-sidebar";
import { SelectionProvider } from "./selection-context";
import { RecentTasksResults } from "./recent-tasks-results";
import { TasksGridSkeleton } from "./tasks-grid-skeleton";

// Reads auth + searchParams, so the route is always dynamically rendered.
// Forcing it also avoids the static-prerender error for the client components
// that call useSearchParams (sidebar, toolbar).
export const dynamic = "force-dynamic";

type RawSearchParams = Record<string, string | string[] | undefined>;

async function getFacets(): Promise<TaskBrowseFacets | null> {
  try {
    const authObj = await auth();
    if (!authObj?.userId) return null;
    const token = await getClerkToken(authObj.getToken);
    if (!token) return null;
    const res = await fetch(getBackendUrl("tasks/browse/facets"), {
      cache: "no-store",
      headers: getAuthHeaders(token),
    });
    if (!res.ok) {
      console.error(
        `[tasks/page] facets fetch failed: ${res.status} - ${await res
          .text()
          .catch(() => "")}`
      );
      return null;
    }
    return (await res.json()) as TaskBrowseFacets;
  } catch (error) {
    console.error("[tasks/page] facets fetch failed", error);
    return null;
  }
}

function first(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

function suspenseKeyFor(searchParams: RawSearchParams): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(searchParams)) {
    const single = first(value);
    if (single != null) params.set(key, single);
  }
  return params.toString();
}

export default async function TasksPage({
  searchParams,
}: {
  searchParams?: Promise<RawSearchParams>;
}) {
  const { orgId } = await auth();
  const sp = (await searchParams) ?? {};
  const facets = await getFacets();

  const offset = Math.max(Number(first(sp.offset) ?? "0") || 0, 0);
  const page = Math.floor(offset / TASKS_PAGE_SIZE) + 1;

  return (
    <SelectionProvider>
      <TooltipProvider>
        <div className="space-y-6">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-start">
            <TasksFilterSidebar facets={facets} />
            <div className="min-w-0 flex-1">
              <Card className="border-[#6f88b4]/20 shadow-xs">
                <CardHeader className="flex flex-col gap-3 pb-3 sm:flex-row sm:items-center sm:justify-between">
                  <div className="space-y-1">
                    <CardTitle className="text-base">Recent Tasks</CardTitle>
                    <div className="text-muted-foreground text-[11px]">
                      Page {page}
                    </div>
                  </div>
                  <TasksToolbar orgId={orgId ?? null} />
                </CardHeader>
                <CardContent className="space-y-4">
                  <Suspense
                    key={suspenseKeyFor(sp)}
                    fallback={<TasksGridSkeleton />}
                  >
                    <RecentTasksResults searchParams={sp} />
                  </Suspense>
                </CardContent>
              </Card>
            </div>
          </div>
        </div>
      </TooltipProvider>
    </SelectionProvider>
  );
}
