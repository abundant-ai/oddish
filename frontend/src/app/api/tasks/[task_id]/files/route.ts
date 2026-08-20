import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import {
  attachUpstreamServerTiming,
  backendFetchHeaders,
} from "@/lib/proxy-headers";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ task_id: string }> },
) {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);

    const { task_id } = await params;

    const search = request.nextUrl.search;
    const url = getBackendUrl("tasks", `/${task_id}/files${search}`);
    const res = await fetch(url, {
      headers: backendFetchHeaders(request, getAuthHeaders(token)),
    });

    if (!res.ok) {
      const error = await res.json().catch(() => ({ detail: res.statusText }));
      return attachUpstreamServerTiming(
        NextResponse.json(error, { status: res.status }),
        res,
      );
    }

    // Presigned URLs expire in 15 min, so cache for 10 min to be safe.
    // This allows the browser to reuse the listing without hitting the backend.
    const cacheControl = "private, max-age=600, stale-while-revalidate=60";

    // Streamed listings (stream=1) are NDJSON — pass the body through so the
    // client can paint the tree before the file contents finish loading.
    const contentType = res.headers.get("content-type") ?? "";
    if (contentType.includes("application/x-ndjson")) {
      return attachUpstreamServerTiming(
        new NextResponse(res.body, {
          headers: {
            "Content-Type": "application/x-ndjson",
            "Cache-Control": cacheControl,
          },
        }),
        res,
      );
    }

    const data = await res.json();
    return attachUpstreamServerTiming(
      NextResponse.json(data, {
        headers: {
          "Cache-Control": cacheControl,
        },
      }),
      res,
    );
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
