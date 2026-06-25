import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

export const maxDuration = 30;

export async function GET(request: NextRequest) {
  try {
    const authObj = await auth();
    if (!authObj || !authObj.userId) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const token = await getClerkToken(authObj.getToken);
    if (!token) {
      return NextResponse.json(
        { error: "Failed to get authentication token" },
        { status: 401 },
      );
    }

    const usageMinutes = request.nextUrl.searchParams.get("usage_minutes");
    const params: Record<string, string> = {};
    if (usageMinutes) params.usage_minutes = usageMinutes;

    const url = getBackendUrl("cost-breakdown", "", params);
    const res = await fetch(url, {
      cache: "no-store",
      headers: getAuthHeaders(token),
      signal: AbortSignal.timeout(25_000),
    });

    if (!res.ok) {
      const errorText = await res.text();
      console.error(`[cost-breakdown] Backend error: ${res.status} - ${errorText}`);
      return NextResponse.json(
        { error: "Failed to fetch cost breakdown", details: errorText },
        { status: res.status },
      );
    }

    const data = await res.json();
    const response = NextResponse.json(data);
    response.headers.set(
      "Cache-Control",
      "private, max-age=5, stale-while-revalidate=30",
    );
    return response;
  } catch (error) {
    console.error("Cost breakdown API route error:", error);
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
