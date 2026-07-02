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
      `/${token}/tasks/${task_id}`,
    );
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
