import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

/**
 * Accepts a raw YAML body and forwards it to the backend's
 * ``POST /reports.yaml`` endpoint, which parses + validates server-side.
 * Used by the dashboard's "Create report" dialog so the textarea
 * contents go straight to the same pydantic / pyyaml stack the CLI
 * uses — no ``yaml`` package needed on the client.
 */
export async function POST(request: NextRequest) {
  const a = await auth();
  if (!a?.userId)
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  const token = await getClerkToken(a.getToken);
  if (!token)
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });

  const body = await request.text();
  const res = await fetch(getBackendUrl("reports.yaml"), {
    method: "POST",
    cache: "no-store",
    headers: {
      "Content-Type": "application/yaml",
      ...getAuthHeaders(token),
    },
    body,
  });
  const text = await res.text();
  return new NextResponse(text, {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
}
