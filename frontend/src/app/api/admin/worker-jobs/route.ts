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
    const params: Record<string, string> = {};
    const staleAfterMinutes = searchParams.get("stale_after_minutes");
    if (staleAfterMinutes) params.stale_after_minutes = staleAfterMinutes;
    const sampleLimit = searchParams.get("sample_limit");
    if (sampleLimit) params.sample_limit = sampleLimit;

    const url = getBackendUrl("admin/worker-jobs", "", params);

    const res = await fetch(url, {
      cache: "no-store",
      headers: getAuthHeaders(token),
    });

    if (!res.ok) {
      const errorText = await res.text();
      console.error(
        `[admin/worker-jobs] Backend error: ${res.status} - ${errorText}`,
      );
      return NextResponse.json(
        { error: "Failed to fetch worker jobs", details: errorText },
        { status: res.status },
      );
    }

    const data = await res.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error("Admin worker-jobs API route error:", error);
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
