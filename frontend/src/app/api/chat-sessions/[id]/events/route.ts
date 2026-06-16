import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    const { id } = await params;
    const authObj = await auth();
    if (!authObj || !authObj.userId) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }
    const t = await getClerkToken(authObj.getToken);
    if (!t) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }
    const qs = request.nextUrl.searchParams.toString();
    const base = getBackendUrl("chat-sessions", `/${id}/events`);
    const res = await fetch(qs ? `${base}?${qs}` : base, {
      cache: "no-store",
      headers: getAuthHeaders(t),
    });
    const text = await res.text();
    return NextResponse.json(text ? JSON.parse(text) : {}, {
      status: res.status,
    });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
