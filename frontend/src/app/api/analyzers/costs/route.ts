import { NextRequest } from "next/server";
import { proxyBackendJson } from "@/lib/backend-response";

export async function GET(request: NextRequest) {
  const windowDays = request.nextUrl.searchParams.get("window_days") ?? "30";
  return proxyBackendJson({
    path: `analyzers/costs?window_days=${encodeURIComponent(windowDays)}`,
  });
}
