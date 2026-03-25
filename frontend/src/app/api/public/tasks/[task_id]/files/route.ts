import { NextRequest, NextResponse } from "next/server";
import { getBackendUrl } from "@/lib/backend-config";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ task_id: string }> },
) {
  try {
    const { task_id } = await params;
    const searchParams = request.nextUrl.searchParams;
    const queryString = searchParams.toString();
    const baseUrl = getBackendUrl("public/tasks", `/${task_id}/files`);
    const url = queryString ? `${baseUrl}?${queryString}` : baseUrl;

    const res = await fetch(url, { cache: "no-store" });
    const text = await res.text();
    const data = text ? JSON.parse(text) : null;

    if (!res.ok) {
      return NextResponse.json(data ?? { error: "Upstream error" }, {
        status: res.status,
      });
    }

    return NextResponse.json(data);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
