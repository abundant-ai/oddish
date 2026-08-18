import { NextRequest, NextResponse } from "next/server";
import { proxyBackendJson } from "@/lib/backend-response";

export async function GET(request: NextRequest) {
  return proxyBackendJson({ request, path: "quotas/org" });
}

export async function PUT(request: NextRequest) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
  }
  return proxyBackendJson({
    request,
    path: "quotas/org",
    method: "PUT",
    body,
  });
}
