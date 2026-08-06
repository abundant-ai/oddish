import { NextRequest, NextResponse } from "next/server";
import { getBackendUrl } from "@/lib/backend-config";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ token: string; task_id: string }> },
) {
  try {
    const { token, task_id } = await params;
    const queryString = request.nextUrl.searchParams.toString();
    const baseUrl = getBackendUrl(
      "public/experiments",
      `/${token}/tasks/${task_id}/files`,
    );
    const url = queryString ? `${baseUrl}?${queryString}` : baseUrl;

    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) {
      const error = await res.json().catch(() => ({ detail: res.statusText }));
      return NextResponse.json(error, { status: res.status });
    }

    const cacheControl = "public, max-age=600, stale-while-revalidate=60";

    // Streamed listings (stream=1) are NDJSON — pass the body through so the
    // client can paint the tree before the file contents finish loading.
    const contentType = res.headers.get("content-type") ?? "";
    if (contentType.includes("application/x-ndjson")) {
      return new NextResponse(res.body, {
        headers: {
          "Content-Type": "application/x-ndjson",
          "Cache-Control": cacheControl,
        },
      });
    }

    const data = await res.json();
    return NextResponse.json(data, {
      headers: {
        "Cache-Control": cacheControl,
      },
    });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
