import { NextRequest, NextResponse } from "next/server";
import { proxyBackendJson } from "@/lib/backend-response";

export async function PUT(
  request: NextRequest,
  { params }: { params: Promise<{ user_id: string }> },
) {
  const { user_id } = await params;
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
  }
  return proxyBackendJson({
    path: `quotas/${encodeURIComponent(user_id)}`,
    method: "PUT",
    body,
  });
}
