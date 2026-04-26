type DashboardRequestParams = {
  tasks_limit?: number;
  tasks_offset?: number;
  experiments_limit?: number;
  experiments_offset?: number;
  experiments_query?: string;
  experiments_status?: string;
  usage_minutes?: number | null;
  include_tasks?: boolean;
  include_usage?: boolean;
  include_experiments?: boolean;
};

export const DASHBOARD_DEFAULT_EXPERIMENTS_LIMIT = 25;
export const DASHBOARD_DEFAULT_USAGE_MINUTES = 1440;

export const DEFAULT_DASHBOARD_REQUEST_PARAMS: DashboardRequestParams =
  Object.freeze({
    include_tasks: false,
    usage_minutes: DASHBOARD_DEFAULT_USAGE_MINUTES,
    experiments_limit: DASHBOARD_DEFAULT_EXPERIMENTS_LIMIT,
    experiments_offset: 0,
    experiments_status: "all",
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

  const trimmedQuery = input.experiments_query?.trim();
  if (trimmedQuery) {
    params.set("experiments_query", trimmedQuery);
  }

  if (input.usage_minutes !== undefined && input.usage_minutes !== null) {
    params.set("usage_minutes", String(input.usage_minutes));
  }

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

export function isDefaultDashboardExperimentsView(
  offset: number,
  query: string,
  status: string,
): boolean {
  return offset === 0 && query.trim().length === 0 && status === "all";
}
