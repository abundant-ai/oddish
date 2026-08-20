import { NextRequest } from "next/server";
import { proxyBackendJson } from "@/lib/backend-response";

// Same-origin proxy for the sidebar experiment filter's async options:
// `query` narrows by name substring, `ids` hydrates already-selected chips,
// `limit` bounds the page. Replaces the deprecated (always empty)
// `facets.experiments` list, which shipped every org experiment.
export async function GET(request: NextRequest) {
  const params = new URLSearchParams();
  for (const key of ["query", "ids", "limit"] as const) {
    const value = request.nextUrl.searchParams.get(key);
    if (value) params.set(key, value);
  }
  const query = params.toString();
  return proxyBackendJson({
    request,
    path: `tasks/browse/experiment-options${query ? `?${query}` : ""}`,
  });
}
