import { parseTaskSearch } from "@/lib/tag-query";
type DashboardRequestParams = {
  tasks_limit?: number;
  tasks_offset?: number;
  experiments_limit?: number;
  experiments_offset?: number;
  experiments_query?: string;
  experiments_status?: string;
  experiments_tags?: string;
  experiments_tags_any?: string;
  experiments_tags_none?: string;
  experiments_models?: string;
  experiments_min_steps?: number;
  experiments_max_steps?: number;
  experiments_min_duration_seconds?: number;
  experiments_max_duration_seconds?: number;
  experiments_min_tool_calls?: number;
  experiments_max_tool_calls?: number;
  experiments_tool_names?: string;
  experiments_tool_count_mins?: string;
  experiments_trial_metric_match?: string;
  experiments_author?: string;
  experiments_author_query?: string;
  usage_minutes?: number | null;
  include_queues?: boolean;
  include_tasks?: boolean;
  include_usage?: boolean;
  include_experiments?: boolean;
};

export const DASHBOARD_DEFAULT_EXPERIMENTS_LIMIT = 25;
export const DASHBOARD_DEFAULT_USAGE_MINUTES = 1440;

// Owner filter sentinel for the experiments table. "all" shows the whole
// organization; "me" scopes to the current user; any other value is an
// org member's user id.
export const DASHBOARD_DEFAULT_EXPERIMENTS_AUTHOR = "me";

export const DEFAULT_DASHBOARD_REQUEST_PARAMS: DashboardRequestParams =
  Object.freeze({
    include_tasks: false,
    usage_minutes: DASHBOARD_DEFAULT_USAGE_MINUTES,
    experiments_limit: DASHBOARD_DEFAULT_EXPERIMENTS_LIMIT,
    experiments_offset: 0,
    experiments_status: "all",
    experiments_author: DASHBOARD_DEFAULT_EXPERIMENTS_AUTHOR,
  });

function setBooleanParam(
  params: URLSearchParams,
  name: string,
  value: boolean | undefined
) {
  if (value !== undefined) {
    params.set(name, String(value));
  }
}

function buildDashboardSearchParams(
  input: DashboardRequestParams
): URLSearchParams {
  const params = new URLSearchParams();

  if (input.tasks_limit !== undefined) {
    params.set("tasks_limit", String(input.tasks_limit));
  }
  if (input.tasks_offset !== undefined) {
    params.set("tasks_offset", String(input.tasks_offset));
  }
  if (input.experiments_limit !== undefined) {
    params.set("experiments_limit", String(input.experiments_limit));
  }
  if (input.experiments_offset !== undefined) {
    params.set("experiments_offset", String(input.experiments_offset));
  }
  if (input.experiments_status) {
    params.set("experiments_status", input.experiments_status);
  }
  // Emit "me" and member ids; omit only for org-wide ("all") so SSR/SWR match
  // the backend filter when defaulting to Mine.
  if (input.experiments_author && input.experiments_author !== "all") {
    params.set("experiments_author", input.experiments_author);
  }

  const trimmedQuery = input.experiments_query?.trim();
  if (trimmedQuery) {
    params.set("experiments_query", trimmedQuery);
  }

  const trimmedAuthorQuery = input.experiments_author_query?.trim();
  if (trimmedAuthorQuery) {
    params.set("experiments_author_query", trimmedAuthorQuery);
  }

  for (const name of [
    "experiments_tags",
    "experiments_tags_any",
    "experiments_tags_none",
  ] as const) {
    const value = input[name]?.trim();
    if (value) {
      params.set(name, value);
    }
  }
  for (const name of [
    "experiments_models",
    "experiments_trial_metric_match",
    "experiments_tool_names",
    "experiments_tool_count_mins",
  ] as const) {
    const value = input[name]?.trim();
    if (value) params.set(name, value);
  }
  for (const name of [
    "experiments_min_steps",
    "experiments_max_steps",
    "experiments_min_duration_seconds",
    "experiments_max_duration_seconds",
    "experiments_min_tool_calls",
    "experiments_max_tool_calls",
  ] as const) {
    const value = input[name];
    if (value !== undefined) params.set(name, String(value));
  }

  if (input.usage_minutes !== undefined && input.usage_minutes !== null) {
    params.set("usage_minutes", String(input.usage_minutes));
  }

  setBooleanParam(params, "include_queues", input.include_queues);
  setBooleanParam(params, "include_tasks", input.include_tasks);
  setBooleanParam(params, "include_usage", input.include_usage);
  setBooleanParam(params, "include_experiments", input.include_experiments);

  return params;
}

export function buildDashboardApiPath(input: DashboardRequestParams): string {
  const query = buildDashboardSearchParams(input).toString();
  return query.length > 0 ? `/api/dashboard?${query}` : "/api/dashboard";
}

export function buildDashboardBackendParams(
  input: DashboardRequestParams
): Record<string, string> {
  return Object.fromEntries(buildDashboardSearchParams(input).entries());
}

/** One URL parser for the experiment list's request and cache identity. */
export function dashboardExperimentsRequest(
  searchParams: URLSearchParams
): DashboardRequestParams {
  const params = Object.fromEntries(searchParams.entries());
  const firstParam = (value: string | undefined) => value ?? "";
  const initialAuthor = params.author || DASHBOARD_DEFAULT_EXPERIMENTS_AUTHOR;
  const initialStatus = params.status || "all";
  const initialQuery = params.q || "";
  const metricNumber = (key: string) => {
    const raw = firstParam(params[key]);
    const value = Number(raw);
    return raw && Number.isFinite(value) && value >= 0 ? value : undefined;
  };
  // TrialMetricFilter rejects min > max; hand-edited URLs shouldn't fail the
  // whole experiments fetch, so swap inverted pairs instead.
  const metricRange = (minKey: string, maxKey: string) => {
    const min = metricNumber(minKey);
    const max = metricNumber(maxKey);
    return min !== undefined && max !== undefined && min > max
      ? ([max, min] as const)
      : ([min, max] as const);
  };
  const [minSteps, maxSteps] = metricRange("min_steps", "max_steps");
  const [minTime, maxTime] = metricRange(
    "min_duration_seconds",
    "max_duration_seconds"
  );
  const [minTools, maxTools] = metricRange("min_tool_calls", "max_tool_calls");
  const pageNumber = Math.max(
    1,
    Number.parseInt(firstParam(params.page), 10) || 1
  );
  const initialOffset = (pageNumber - 1) * DASHBOARD_DEFAULT_EXPERIMENTS_LIMIT;

  const parsedQuery = parseTaskSearch(initialQuery);
  return {
    ...DEFAULT_DASHBOARD_REQUEST_PARAMS,
    include_tasks: false,
    include_usage: false,
    include_queues: false,
    include_experiments: true,
    experiments_offset: initialOffset,
    experiments_author: initialAuthor,
    experiments_status: initialStatus,
    experiments_query: parsedQuery.text,
    experiments_tags: parsedQuery.all.join(","),
    experiments_tags_any: parsedQuery.any.join(","),
    experiments_tags_none: parsedQuery.none.join(","),
    experiments_author_query: parsedQuery.authors.join(","),
    experiments_models: firstParam(params.models) || undefined,
    experiments_min_steps: minSteps,
    experiments_max_steps: maxSteps,
    experiments_min_duration_seconds: minTime,
    experiments_max_duration_seconds: maxTime,
    experiments_min_tool_calls: minTools,
    experiments_max_tool_calls: maxTools,
    experiments_tool_names: firstParam(params.tool_names) || undefined,
    experiments_trial_metric_match:
      firstParam(params.trial_metric_match) === "all" ? "all" : "any",
  };
}
