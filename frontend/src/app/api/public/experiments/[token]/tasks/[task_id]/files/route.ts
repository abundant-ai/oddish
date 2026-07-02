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

    const data = await res.json();
    return NextResponse.json(data, {
      headers: {
        "Cache-Control": "public, max-age=600, stale-while-revalidate=60",
      },
    });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
