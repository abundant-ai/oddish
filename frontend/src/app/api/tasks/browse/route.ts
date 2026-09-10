import { NextRequest } from "next/server";
import { proxyBackendJson } from "@/lib/backend-response";
import { buildBrowseQuery } from "@/lib/browse-proxy-query";

// Same-origin proxy for the task grid's client-side browse fetch (see
// lib/use-task-browse.ts — one fetch per filter state, cached between
// visits). The request arrives in display form — the page URL's filter
// params, rolling *_within presets still as tokens — and is resolved here,
// per request, into the backend query. The resolution ran in the
// server-rendered grid before (recent-tasks-results.tsx) and keeps its
// exact meaning; only the route it runs in changed.
//
// The matching-task count is a sibling route (browse/count) rather than a
// flag here, so "one browse fetch per filter state" stays a property this
// URL alone can be measured against.
export async function GET(request: NextRequest) {
  const query = buildBrowseQuery(request.nextUrl.searchParams);

  // The request signal rides along so a client abort (superseded filter
  // state, timeout) cancels the backend query instead of letting it run
  // for a result nobody will render.
  return proxyBackendJson({
    request,
    path: `tasks/browse?${query.toString()}`,
    signal: request.signal,
  });
}
