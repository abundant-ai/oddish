import { NextRequest, NextResponse } from "next/server";
import { proxyBackendJson } from "@/lib/backend-response";

export async function GET() {
  return proxyBackendJson({ path: "quotas/duels" });
}

export async function POST(request: NextRequest) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
  }
  return proxyBackendJson({ path: "quotas/duels", method: "POST", body });
}
