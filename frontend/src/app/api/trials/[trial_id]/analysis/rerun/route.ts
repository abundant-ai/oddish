import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

// Queue analysis for one trial. Classifies only this trial. Does not
// touch other trials, the task verdict, or the pre-trial audit.
export async function POST(
  _request: Request,
  { params }: { params: Promise<{ trial_id: string }> },
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

    const { trial_id } = await params;
    const url = getBackendUrl("trials", `/${trial_id}/analysis/rerun`);
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
