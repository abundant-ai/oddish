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

    const page = searchParams.get("page");
    const pageSize = searchParams.get("page_size");
    const status = searchParams.get("status");
    const entrypoint = searchParams.get("entrypoint");

    if (page) params.page = page;
    if (pageSize) params.page_size = pageSize;
    if (status) params.status = status;
    if (entrypoint) params.entrypoint = entrypoint;

    const url = getBackendUrl("admin/pgqueuer", "", params);

    const res = await fetch(url, {
      cache: "no-store",
      headers: getAuthHeaders(token),
    });

    if (!res.ok) {
      const errorText = await res.text();
      console.error(
        `[admin/pgqueuer] Backend error: ${res.status} - ${errorText}`,
      );
      return NextResponse.json(
        { error: "Failed to fetch pgqueuer jobs", details: errorText },
        { status: res.status },
      );
    }

    const data = await res.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error("Admin pgqueuer API route error:", error);
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
