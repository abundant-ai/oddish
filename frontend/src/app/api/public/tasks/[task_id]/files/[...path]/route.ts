import { NextRequest, NextResponse } from "next/server";
import { getBackendUrl } from "@/lib/backend-config";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ task_id: string; path: string[] }> },
) {
  try {
    const { task_id, path } = await params;
    const filePath = path.join("/");
    const search = request.nextUrl.search;

    const url = getBackendUrl(
      "public/tasks",
      `/${task_id}/files/${filePath}${search}`,
    );
    const res = await fetch(url, { cache: "no-store" });

    if (!res.ok) {
      const error = await res.json().catch(() => ({ detail: res.statusText }));
      return NextResponse.json(error, { status: res.status });
    }

    const data = await res.json();

    return NextResponse.json(data, {
      headers: {
        "Cache-Control": "public, max-age=300, stale-while-revalidate=60",
      },
    });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
