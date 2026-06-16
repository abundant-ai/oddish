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

async function authedToken() {
  const authObj = await auth();
  if (!authObj || !authObj.userId) {
    return { error: "Unauthorized" as const, status: 401 };
  }
  const token = await getClerkToken(authObj.getToken);
  if (!token) {
    return { error: "Failed to get authentication token" as const, status: 401 };
  }
  return { token };
}

export async function POST(request: NextRequest) {
  try {
    const a = await authedToken();
    if ("error" in a) {
      return NextResponse.json({ error: a.error }, { status: a.status });
    }
    const body = await request.json();
    const res = await fetch(getBackendUrl("chat-sessions"), {
      method: "POST",
      cache: "no-store",
      headers: {
        ...getAuthHeaders(a.token),
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
    const text = await res.text();
    const payload = safeJson(text);
    return NextResponse.json(payload, { status: res.status });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}

export async function GET(request: NextRequest) {
  try {
    const a = await authedToken();
    if ("error" in a) {
      return NextResponse.json({ error: a.error }, { status: a.status });
    }
    const qs = request.nextUrl.searchParams.toString();
    const base = getBackendUrl("chat-sessions");
    const res = await fetch(qs ? `${base}?${qs}` : base, {
      cache: "no-store",
      headers: getAuthHeaders(a.token),
    });
    const text = await res.text();
    const payload = safeJson(text);
    return NextResponse.json(payload, { status: res.status });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
