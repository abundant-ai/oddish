import { getAuthHeaders, getBackendUrl } from "@/lib/backend-config";
import type { CostBreakdownResponse } from "@/lib/types";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { CostBreakdownCard } from "@/components/cost-breakdown-card";

// Window/limits MUST match the CostBreakdownCard's initial SWR key (window "7",
// limit 500) so the fetched payload is used as the fallback rather than
// triggering an immediate client refetch.
const COST_SSR_WINDOW_DAYS = "7";
const COST_SSR_LIMIT = "500";

async function getInitialCostData(
  token: string,
): Promise<CostBreakdownResponse | null> {
  try {
    const url = getBackendUrl("admin/costs", "", {
      window_days: COST_SSR_WINDOW_DAYS,
      experiment_limit: COST_SSR_LIMIT,
      user_limit: COST_SSR_LIMIT,
    });
    const response = await fetch(url, {
      cache: "no-store",
      headers: getAuthHeaders(token),
    });
    if (!response.ok) {
      console.error(`[usage/costing] Failed cost fetch: ${response.status}`);
      return null;
    }
    return (await response.json()) as CostBreakdownResponse;
  } catch (error) {
    console.error("[usage/costing] Cost fetch failed", error);
    return null;
  }
}

// Async server component: awaits the heavy (global) cost query and renders the
// card. Wrapped in <Suspense> by the page so navigating to the Costing tab
// shows the skeleton fallback immediately and streams the data in when ready,
// rather than blocking the tab switch.
export async function CostingPanel({ token }: { token: string }) {
  const data = await getInitialCostData(token);
  return (
    <CostBreakdownCard
      initialData={data}
      showChart={false}
      enableSearch
      experimentLimit={500}
      userLimit={500}
    />
  );
}

export function CostingSkeleton() {
  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <Skeleton className="h-5 w-40" />
          <Skeleton className="h-8 w-[240px] rounded-md" />
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap gap-2">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-6 w-24" />
          ))}
        </div>
        <div className="space-y-2">
          {Array.from({ length: 8 }).map((_, i) => (
            <Skeleton key={i} className="h-9" />
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
