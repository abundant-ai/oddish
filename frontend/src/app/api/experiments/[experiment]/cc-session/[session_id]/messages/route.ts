import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import { getAuthHeaders, getClerkToken } from "@/lib/backend-config";
import { decodeExperimentRouteParam } from "@/lib/utils";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function POST(
  request: Request,
  {
    params,
  }: {
    params: Promise<{ experiment: string; session_id: string }>;
  },
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

    const { experiment, session_id } = await params;
    const experimentId = decodeExperimentRouteParam(experiment);
    const url = `${API_URL}/api/experiments/${encodeURIComponent(
      experimentId,
    )}/cc-session/${encodeURIComponent(session_id)}/messages`;

    const body = await request.text();

    const upstream = await fetch(url, {
      method: "POST",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        ...getAuthHeaders(token),
      },
      body,
    });

    if (!upstream.ok) {
      const text = await upstream.text();
      const data = text ? safeJson(text) : null;
      return NextResponse.json(data ?? { error: "Upstream error" }, {
        status: upstream.status,
      });
    }

    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "Content-Type":
          upstream.headers.get("content-type") ?? "text/event-stream",
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
      },
    });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return { error: text };
  }
}
