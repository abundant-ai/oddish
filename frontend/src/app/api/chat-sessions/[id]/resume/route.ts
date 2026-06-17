import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import { getAuthHeaders, getBackendUrl, getClerkToken } from "@/lib/backend-config";

export async function POST(_req: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const authObj = await auth();
  if (!authObj || !authObj.userId) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  const t = await getClerkToken(authObj.getToken);
  if (!t) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  const res = await fetch(getBackendUrl("chat-sessions", `/${id}/resume`), {
    method: "POST",
    cache: "no-store",
    headers: getAuthHeaders(t),
  });
  if (res.status === 204) return new NextResponse(null, { status: 204 });
  const text = await res.text();
  return NextResponse.json(
    text ? safeJson(text) : { detail: "resume failed" },
    { status: res.status },
  );
}

function safeJson(text: string): unknown {
  try { return JSON.parse(text); } catch { return { detail: text }; }
}
