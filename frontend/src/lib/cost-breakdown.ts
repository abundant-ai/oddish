import useSWR from "swr";
import { fetcher } from "@/lib/api";

// One finest-grain row per (model, experiment), carrying the experiment owner
// so the client can roll spend up by model, experiment, or user without
// refetching. Tokens are included (free to compute) but not rendered today.
export interface CostBreakdownRow {
  model: string;
  provider: string;
  experiment_id: string | null;
  experiment_name: string;
  owner_user_id: string | null;
  owner_label: string;
  cost_usd: number;
  cost_estimated_usd: number;
  input_tokens: number;
  output_tokens: number;
  cache_tokens: number;
  trial_count: number;
  running: number;
  queued: number;
  retrying: number;
  is_other?: boolean;
}

export interface CostBreakdownResponse {
  usage_minutes: number;
  rows: CostBreakdownRow[];
}

export const COST_DEFAULT_USAGE_MINUTES = 1440;

export function buildCostBreakdownApiPath(usageMinutes: number): string {
  const params = new URLSearchParams({ usage_minutes: String(usageMinutes) });
  return `/api/cost-breakdown?${params.toString()}`;
}

// Only the time window triggers a refetch; every group-by / layout / search
// change is a pure client-side re-aggregation of the rows we already hold.
export function useCostBreakdown(usageMinutes: number) {
  const { data, error, isLoading, isValidating } = useSWR<CostBreakdownResponse>(
    buildCostBreakdownApiPath(usageMinutes),
    fetcher,
    {
      revalidateOnFocus: false,
      keepPreviousData: true,
      refreshInterval: 60000,
    },
  );

  return {
    rows: data?.rows ?? [],
    usageMinutes: data?.usage_minutes ?? usageMinutes,
    error,
    isLoading,
    isRefreshing: !error && !isLoading && isValidating,
  };
}
