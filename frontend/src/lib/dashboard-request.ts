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

// Max time the SSR initial fetch waits on the backend before falling
// back to client fetching.
export const DASHBOARD_SSR_FETCH_TIMEOUT_MS = 5_000;

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
  value: boolean | undefined,
) {
  if (value !== undefined) {
    params.set(name, String(value));
  }
}

function buildDashboardSearchParams(
  input: DashboardRequestParams,
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
  input: DashboardRequestParams,
): Record<string, string> {
  return Object.fromEntries(buildDashboardSearchParams(input).entries());
}
