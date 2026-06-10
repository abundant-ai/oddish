import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

// Proxies the backend org-members list used by the dashboard owner
// filter (member picker). The backend gates this on admin/owner role, so
// non-admins receive the upstream 403 — the dashboard degrades to the
// Org / Mine toggle in that case.
export async function GET() {
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

    const res = await fetch(getBackendUrl("users"), {
      cache: "no-store",
      headers: getAuthHeaders(token),
    });

    if (!res.ok) {
      const errorText = await res.text();
      return NextResponse.json(
        { error: "Failed to fetch users", details: errorText },
        { status: res.status },
      );
    }

    const data = await res.json();
    const response = NextResponse.json(data);
    response.headers.set(
      "Cache-Control",
      "private, max-age=30, stale-while-revalidate=120",
    );
    return response;
  } catch (error) {
    console.error("Users API route error:", error);
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
