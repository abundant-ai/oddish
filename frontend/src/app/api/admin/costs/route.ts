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
        { status: 401 }
      );
    }

    const searchParams = request.nextUrl.searchParams;
    const params: Record<string, string> = {};
    const windowDays = searchParams.get("window_days");
    if (windowDays !== null) params.window_days = windowDays;
    const experimentLimit = searchParams.get("experiment_limit");
    if (experimentLimit) params.experiment_limit = experimentLimit;
    const userLimit = searchParams.get("user_limit");
    if (userLimit) params.user_limit = userLimit;

    const url = getBackendUrl("admin/costs", "", params);

    const res = await fetch(url, {
      cache: "no-store",
      headers: getAuthHeaders(token),
    });

    if (!res.ok) {
      const errorText = await res.text();
      console.error(
        `[admin/costs] Backend error: ${res.status} - ${errorText}`
      );
      return NextResponse.json(
        { error: "Failed to fetch cost breakdown", details: errorText },
        { status: res.status }
      );
    }

    const data = await res.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error("Admin costs API route error:", error);
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 }
    );
  }
}
