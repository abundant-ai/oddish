import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

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

    const url = getBackendUrl("tasks", `/${task_id}/verdict/retry`);
    const res = await fetch(url, {
      method: "POST",
      headers: getAuthHeaders(token),
    });

    const text = await res.text();
    const data = text ? JSON.parse(text) : null;

    if (!res.ok) {
      return NextResponse.json(
        data ?? { error: "Failed to queue task verdict" },
        {
          status: res.status,
        }
      );
    }

    return NextResponse.json(data);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 }
    );
  }
}
