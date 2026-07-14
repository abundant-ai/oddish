import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

export async function PUT(
  _request: NextRequest,
  { params }: { params: Promise<{ task_id: string; version: string }> }
) {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);
    const { task_id, version } = await params;
    const url = getBackendUrl(
      "tasks",
      `/${encodeURIComponent(task_id)}/versions/${encodeURIComponent(version)}/default`
    );
    const res = await fetch(url, {
      method: "PUT",
      headers: getAuthHeaders(token),
    });

    const data = await res.json().catch(() => ({ error: "Upstream error" }));
    return NextResponse.json(data, { status: res.status });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 }
    );
  }
}
