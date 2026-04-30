import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import { getAuthHeaders, getClerkToken } from "@/lib/backend-config";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function GET(
  _request: Request,
  {
    params,
  }: { params: Promise<{ task_id: string; session_id: string }> },
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
    const { task_id, session_id } = await params;
    const url = `${API_URL}/api/tasks/${encodeURIComponent(
      task_id,
    )}/cc-session/${encodeURIComponent(session_id)}/skills.tar.gz`;
    const upstream = await fetch(url, {
      method: "GET",
      cache: "no-store",
      headers: getAuthHeaders(token),
    });
    if (!upstream.ok) {
      const text = await upstream.text();
      return NextResponse.json(
        { error: text || "Upstream error" },
        { status: upstream.status },
      );
    }
    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "Content-Type":
          upstream.headers.get("content-type") ?? "application/gzip",
        "Content-Disposition":
          upstream.headers.get("content-disposition") ??
          `attachment; filename="cc-skills-${session_id}.tar.gz"`,
      },
    });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
