import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

export async function POST(request: Request) {
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

    const body = await request.json().catch(() => null);
    if (!body || !Array.isArray(body.task_ids)) {
      return NextResponse.json(
        { error: "Missing task_ids payload" },
        { status: 400 },
      );
    }

    const url = getBackendUrl("tasks", "/cancel");
    const res = await fetch(url, {
      method: "POST",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        ...getAuthHeaders(token),
      },
      body: JSON.stringify({ task_ids: body.task_ids }),
    });

    const text = await res.text();
    // The backend normally returns JSON, but an unhandled 500 yields a
    // plain-text body ("Internal Server Error"). Guard the parse so we
    // forward the real status + message instead of throwing here (which
    // would surface as a misleading "Unexpected token 'I'..." in the UI).
    let data: unknown = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = text ? { error: text } : null;
    }

    if (!res.ok) {
      return NextResponse.json(data ?? { error: "Failed to cancel tasks" }, {
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
