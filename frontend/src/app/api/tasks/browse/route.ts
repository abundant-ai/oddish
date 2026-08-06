import { NextRequest } from "next/server";
import { proxyBackendJson } from "@/lib/backend-response";
import { BROWSE_FORWARD_KEYS } from "@/lib/tasks-filters";

// Same-origin proxy for the task grid's client-side browse fetch (see
// lib/use-task-browse.ts — one fetch per filter state, cached between
// visits). Forwards paging, the parsed search fields, and the filter keys.
// The rolling *_within presets never reach this route: the client resolves
// them to absolute bounds per fetch, and they are not backend params.
const FORWARDED_KEYS = [
  "limit",
  "offset",
  "query",
  "author",
  ...BROWSE_FORWARD_KEYS.filter(
    (key) => key !== "created_within" && key !== "trial_finished_within"
  ),
];

export async function GET(request: NextRequest) {
  const params = new URLSearchParams();
  for (const key of FORWARDED_KEYS) {
    const value = request.nextUrl.searchParams.get(key);
    if (value) params.set(key, value);
  }
  const query = params.toString();
  return proxyBackendJson({
    path: `tasks/browse${query ? `?${query}` : ""}`,
  });
}
