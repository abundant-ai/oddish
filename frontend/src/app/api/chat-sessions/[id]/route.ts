import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

function safeJson(text: string): unknown {
  try {
    return text ? JSON.parse(text) : {};
  } catch {
    return { detail: text };
  }
}

async function token() {
  const authObj = await auth();
  if (!authObj || !authObj.userId) return null;
  return getClerkToken(authObj.getToken);
}

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    const { id } = await params;
    const t = await token();
    if (!t) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }
    const res = await fetch(getBackendUrl("chat-sessions", `/${id}`), {
      cache: "no-store",
      headers: getAuthHeaders(t),
    });
    const text = await res.text();
    return NextResponse.json(safeJson(text), {
      status: res.status,
    });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}

export async function DELETE(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    const { id } = await params;
    const t = await token();
    if (!t) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }
    const res = await fetch(getBackendUrl("chat-sessions", `/${id}`), {
      method: "DELETE",
      cache: "no-store",
      headers: getAuthHeaders(t),
    });
    return new NextResponse(null, { status: res.status });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
