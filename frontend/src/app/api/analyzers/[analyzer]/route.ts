import { NextResponse } from "next/server";
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

async function forward(
  method: "GET" | "DELETE",
  analyzerId: string,
): Promise<NextResponse> {
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
  const url = getBackendUrl("analyzers", `/${encodeURIComponent(analyzerId)}`);
  const res = await fetch(url, {
    method,
    cache: "no-store",
    headers: getAuthHeaders(token),
  });
  const payload = safeJson(await res.text());
  return NextResponse.json(payload, { status: res.status });
}

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ analyzer: string }> },
) {
  try {
    const { analyzer } = await params;
    return await forward("GET", analyzer);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}

export async function DELETE(
  _request: Request,
  { params }: { params: Promise<{ analyzer: string }> },
) {
  try {
    const { analyzer } = await params;
    return await forward("DELETE", analyzer);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
