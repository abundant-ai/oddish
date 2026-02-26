import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

export async function GET(request: NextRequest) {
  try {
    const authObj = await auth();

    if (!authObj || !authObj.userId) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const token = await getClerkToken(authObj.getToken);

    if (!token) {
      console.error("Failed to get Clerk token for user:", authObj.userId);
      return NextResponse.json(
        { error: "Failed to get authentication token" },
        { status: 401 },
      );
    }

    const searchParams = request.nextUrl.searchParams;
    const tasksLimit = searchParams.get("tasks_limit") || "200";
    const tasksOffset = searchParams.get("tasks_offset") || "0";
    const experimentsLimit = searchParams.get("experiments_limit");
    const experimentsOffset = searchParams.get("experiments_offset");
    const experimentsQuery = searchParams.get("experiments_query");
    const experimentsStatus = searchParams.get("experiments_status");
    const usageMinutes = searchParams.get("usage_minutes");
    const includeTasks = searchParams.get("include_tasks");
    const includeUsage = searchParams.get("include_usage");
    const includeExperiments = searchParams.get("include_experiments");
    const params: Record<string, string> = {
      tasks_limit: tasksLimit,
      tasks_offset: tasksOffset,
    };
    if (experimentsLimit) params.experiments_limit = experimentsLimit;
    if (experimentsOffset) params.experiments_offset = experimentsOffset;
    if (experimentsQuery) params.experiments_query = experimentsQuery;
    if (experimentsStatus) params.experiments_status = experimentsStatus;
    if (usageMinutes) params.usage_minutes = usageMinutes;
    if (includeTasks) params.include_tasks = includeTasks;
    if (includeUsage) params.include_usage = includeUsage;
    if (includeExperiments) params.include_experiments = includeExperiments;
    const url = getBackendUrl("dashboard", "", params);

    const res = await fetch(url, {
      cache: "no-store",
      headers: getAuthHeaders(token),
    });

    if (!res.ok) {
      const errorText = await res.text();
      console.error(`[dashboard] Backend error: ${res.status} - ${errorText}`);
      return NextResponse.json(
        { error: "Failed to fetch dashboard", details: errorText },
        { status: res.status },
      );
    }

    const data = await res.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error("Dashboard API route error:", error);
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
