import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

export async function GET(_request: NextRequest) {
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

    const res = await fetch(getBackendUrl("tasks/browse/facets"), {
      cache: "no-store",
      headers: getAuthHeaders(token),
    });

    if (!res.ok) {
      const errorText = await res.text();
      console.error(
        `[tasks/browse/facets] Backend error: ${res.status} - ${errorText}`
      );
      return NextResponse.json(
        { error: "Failed to fetch task filter facets", details: errorText },
        { status: res.status }
      );
    }

    const data = await res.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error("Task browse facets API route error:", error);
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 }
    );
  }
}
