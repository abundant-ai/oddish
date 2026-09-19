import { NextRequest } from "next/server";
import { proxyBackendJson } from "@/lib/backend-response";
import { buildBrowseQuery } from "@/lib/browse-proxy-query";

// Same-origin proxy for "Select all N": the task ids of the whole filter
// set, in page order. Its own path for the same reason as the count route
// (see ./count/route.ts): the grid's one-fetch-per-filter-state network
// shape stays observable, and upstream it is the same backend endpoint with
// ids_only=true, so it can never resolve a filter differently than the page.
export async function GET(request: NextRequest) {
  const query = buildBrowseQuery(request.nextUrl.searchParams, {
    idsOnly: true,
  });

  return proxyBackendJson({
    request,
    path: `tasks/browse?${query.toString()}`,
    signal: request.signal,
  });
}
