import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import { backendErrorPayload, readBackendJson } from "@/lib/backend-response";

export async function POST(
  _request: Request,
  { params }: { params: Promise<{ task_id: string }> }
) {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);

    if (!token) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const { task_id } = await params;

    const url = getBackendUrl("tasks", `/${task_id}/analysis/cancel`);
    const res = await fetch(url, {
      method: "POST",
      headers: getAuthHeaders(token),
    });

    const parsed = await readBackendJson(res, "Failed to cancel task analysis");

    if (parsed.parseError) {
      return NextResponse.json(parsed.parseError, { status: parsed.status });
    }

    if (!res.ok) {
      return NextResponse.json(
        backendErrorPayload(parsed.data, "Failed to cancel task analysis"),
        {
          status: res.status,
        }
      );
    }

    return NextResponse.json(parsed.data);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 }
    );
  }
}
