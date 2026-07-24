import { NextRequest } from "next/server";
import { proxyBackendJson } from "@/lib/backend-response";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ userId: string }> },
) {
  const { userId } = await params;
  const search = request.nextUrl.searchParams;
  const query: Record<string, string> = {};
  const windowDays = search.get("window_days");
  if (windowDays !== null) query.window_days = windowDays;
  const taskLimit = search.get("task_limit");
  if (taskLimit) query.task_limit = taskLimit;
  const qs = new URLSearchParams(query).toString();
  return proxyBackendJson({
    path: `admin/costs/users/${encodeURIComponent(userId)}${qs ? `?${qs}` : ""}`,
  });
}
