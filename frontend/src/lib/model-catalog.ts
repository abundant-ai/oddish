import type { ModelEndpointCheckResponse, ModelEndpointSummary } from "./types";

export type ModelCheckState =
  | { status: "running" }
  | { status: "complete"; result: ModelEndpointCheckResponse; testedAt: number }
  | { status: "error"; message: string; testedAt: number };

export type ModelStatus =
  | "Not tested"
  | "Testing"
  | "Passed"
  | "Failed"
  | "CLI only";
export type ModelSortField =
  | "name"
  | "provider"
  | "status"
  | "latency"
  | "testedAt";
export type ModelSort = { field: ModelSortField; direction: "asc" | "desc" };

export const ROUTE_LABELS: Record<string, string> = {
  "anthropic-hdo": "Anthropic HDO",
  anthropic: "Anthropic",
  azure: "Azure OpenAI",
  bedrock: "AWS Bedrock",
  cursor: "Cursor",
  deepseek: "DeepSeek",
  fireworks: "Fireworks",
  fireworks_ai: "Fireworks AI",
  gemini: "Google Gemini",
  meta: "Meta",
  minimax: "MiniMax",
  moonshot: "Moonshot",
  openai: "OpenAI",
  openrouter: "OpenRouter",
  vertex_ai: "Google Vertex AI",
  xai: "xAI",
  zai: "Z.ai",
};

export function endpointKey(endpoint: ModelEndpointSummary): string {
  return `${endpoint.route}:${endpoint.model}`;
}

// Treat punctuation as word boundaries in both identifiers and search terms.
function searchWords(value: string): string[] {
  return value
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(Boolean);
}

export function modelCatalogRows(
  models: ModelEndpointSummary[],
  checks: Record<string, ModelCheckState>,
  query: string,
  provider: string,
  status: ModelStatus | "all",
  sort: ModelSort,
  includePreviouslyUsed = false
) {
  const terms = searchWords(query);
  return models
    .map((endpoint) => {
      const key = endpointKey(endpoint);
      const check = checks[key];
      const result = check?.status === "complete" ? check.result : null;
      const status: ModelStatus =
        check?.status === "running"
          ? "Testing"
          : check?.status === "error" || result?.ok === false
            ? "Failed"
            : result?.ok
              ? "Passed"
              : endpoint.testable
                ? "Not tested"
                : "CLI only";
      return {
        endpoint,
        key,
        name: endpoint.model.split("/").at(-1)!.replace(/[-_]/g, " "),
        provider: ROUTE_LABELS[endpoint.route] ?? endpoint.route,
        status,
        latency: result?.latency_ms ?? null,
        testedAt: check && check.status !== "running" ? check.testedAt : null,
      };
    })
    .filter((row) => {
      const searchable = searchWords(
        `${row.endpoint.model} ${row.endpoint.provider} ${row.endpoint.route} ${row.provider}`
      ).join(" ");
      return (
        (includePreviouslyUsed || row.endpoint.is_configured) &&
        (provider === "all" || row.endpoint.route === provider) &&
        (status === "all" || row.status === status) &&
        terms.every((term) => searchable.includes(term))
      );
    })
    .sort((a, b) => {
      const left = a[sort.field];
      const right = b[sort.field];
      // Missing measurements belong last in either direction.
      if (left === null && right !== null) return 1;
      if (right === null && left !== null) return -1;
      const order =
        typeof left === "number" && typeof right === "number"
          ? left - right
          : String(left ?? "").localeCompare(String(right ?? ""), undefined, {
              numeric: true,
            });
      return (
        (sort.direction === "asc" ? order : -order) ||
        a.key.localeCompare(b.key)
      );
    });
}
