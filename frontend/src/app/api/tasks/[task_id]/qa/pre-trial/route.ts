import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

// Queue the pre-trial audit for the task's current version. Runs only
// the audit. Does not classify trials and does not synthesize the
// verdict.
export async function POST(
  _request: Request,
  { params }: { params: Promise<{ task_id: string }> },
) {
  try {
    const authObj = await auth();
    if (!authObj || !authObj.userId) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const token = await getClerkToken(authObj.getToken);
    if (!token) {
      return NextResponse.json(
        { error: "Failed to get authentication token" },
        { status: 401 },
      );
    }

    const { task_id } = await params;
    const url = getBackendUrl("tasks", `/${task_id}/qa/pre-trial`);
    const res = await fetch(url, {
      method: "POST",
      cache: "no-store",
      headers: getAuthHeaders(token),
    });

    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      return NextResponse.json(body, { status: res.status });
    }
    return NextResponse.json(body);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
