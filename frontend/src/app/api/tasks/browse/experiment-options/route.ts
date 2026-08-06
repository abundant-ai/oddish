import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

// Same-origin proxy for the sidebar experiment filter's async options:
// `query` narrows by name substring, `ids` hydrates already-selected chips,
// `limit` bounds the page. Replaces the deprecated (always empty)
// `facets.experiments` list, which shipped every org experiment.
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
        { status: 401 }
      );
    }

    const params: Record<string, string> = {};
    for (const key of ["query", "ids", "limit"] as const) {
      const value = request.nextUrl.searchParams.get(key);
      if (value) params[key] = value;
    }

    const res = await fetch(
      getBackendUrl("tasks/browse/experiment-options", "", params),
      {
        cache: "no-store",
        headers: getAuthHeaders(token),
      }
    );

    if (!res.ok) {
      const errorText = await res.text();
      console.error(
        `[tasks/browse/experiment-options] Backend error: ${res.status} - ${errorText}`
      );
      return NextResponse.json(
        { error: "Failed to fetch experiment options", details: errorText },
        { status: res.status }
      );
    }

    return NextResponse.json(await res.json());
  } catch (error) {
    console.error("Experiment options API route error:", error);
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 }
    );
  }
}
