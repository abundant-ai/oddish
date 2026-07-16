import { NextRequest } from "next/server";
import { proxyBackendJson } from "@/lib/backend-response";

export async function GET(request: NextRequest) {
  const params = new URLSearchParams();
  const windowDays = request.nextUrl.searchParams.get("window_days");
  const limit = request.nextUrl.searchParams.get("limit");
  if (windowDays !== null) params.set("window_days", windowDays);
  if (limit !== null) params.set("limit", limit);
  const query = params.toString();
  return proxyBackendJson({ path: `leaderboard${query ? `?${query}` : ""}` });
}
