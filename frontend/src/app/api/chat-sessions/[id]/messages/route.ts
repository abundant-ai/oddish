import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

export const dynamic = "force-dynamic"; // never statically optimize a stream

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  const authObj = await auth();
  if (!authObj || !authObj.userId) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const t = await getClerkToken(authObj.getToken);
  if (!t) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const body = await request.json();
  const res = await fetch(getBackendUrl("chat-sessions", `/${id}/messages`), {
    method: "POST",
    cache: "no-store",
    headers: { ...getAuthHeaders(t), "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!res.ok || !res.body) {
    const text = await res.text();
    return NextResponse.json(
      text ? safeJson(text) : { detail: "chat stream failed" },
      { status: res.ok ? 502 : res.status },
    );
  }

  return new Response(res.body, {
    status: 200,
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
    },
  });
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return { detail: text };
  }
}
