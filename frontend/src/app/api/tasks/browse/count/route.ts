import { NextRequest } from "next/server";
import { proxyBackendJson } from "@/lib/backend-response";
import { buildBrowseQuery } from "@/lib/browse-proxy-query";

// Same-origin proxy for the task browser's matching-task count. Its own path,
// not a flag on the grid route: the count is a separate request on a separate
// cache key (see lib/use-task-browse.ts), and keeping the URLs distinct is
// what lets "one browse fetch per filter state" remain observable in the
// network shape — a count riding on /api/tasks/browse would read as a second
// grid fetch.
//
// Upstream it IS the same backend endpoint with count_only=true, so the count
// shares the page's parameter parsing and can never apply different filters
// than the listing it labels.
export async function GET(request: NextRequest) {
  const query = buildBrowseQuery(request.nextUrl.searchParams, {
    countOnly: true,
  });

  return proxyBackendJson({
    request,
    path: `tasks/browse?${query.toString()}`,
    signal: request.signal,
  });
}
