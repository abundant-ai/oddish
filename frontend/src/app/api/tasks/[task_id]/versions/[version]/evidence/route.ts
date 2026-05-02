import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ task_id: string; version: string }> }
) {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);
    const { task_id, version } = await params;
    const url = getBackendUrl(
      "tasks",
      `/${encodeURIComponent(task_id)}/versions/${encodeURIComponent(version)}/evidence`
    );
    const res = await fetch(url, {
      cache: "no-store",
      headers: getAuthHeaders(token),
    });

    const data = await res.json();
    if (!res.ok) {
      return NextResponse.json(data, { status: res.status });
    }
    return NextResponse.json(data);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 }
    );
  }
}
