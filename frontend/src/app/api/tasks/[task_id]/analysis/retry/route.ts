import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

export async function POST(
  _request: Request,
  { params }: { params: Promise<{ task_id: string }> },
) {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);

    if (!token) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const { task_id } = await params;

    const url = getBackendUrl("tasks", `/${task_id}/analysis/retry`);
    const res = await fetch(url, {
      method: "POST",
      headers: getAuthHeaders(token),
    });

    const text = await res.text();
    let data: unknown = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        const snippet = text.length > 200 ? `${text.slice(0, 200)}…` : text;
        return NextResponse.json(
          { error: `Backend ${res.status}: ${snippet}` },
          { status: res.status >= 400 ? res.status : 502 },
        );
      }
    }

    if (!res.ok) {
      return NextResponse.json(
        data ?? { error: "Failed to queue task analysis" },
        { status: res.status },
      );
    }

    return NextResponse.json(data);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
