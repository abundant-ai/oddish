import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";

import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

export async function GET(
  request: Request,
  { params }: { params: Promise<{ task_id: string }> }
) {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);
    const { task_id } = await params;
    const version = new URL(request.url).searchParams.get("version");
    const url = getBackendUrl(
      "tasks",
      `/${task_id}/qa/runs`,
      version === null ? undefined : { version }
    );
    const response = await fetch(url, {
      cache: "no-store",
      headers: getAuthHeaders(token),
    });
    const text = await response.text();
    const data = text ? JSON.parse(text) : null;
    return NextResponse.json(
      response.ok ? data : (data ?? { error: "Upstream error" }),
      { status: response.status }
    );
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 }
    );
  }
}
