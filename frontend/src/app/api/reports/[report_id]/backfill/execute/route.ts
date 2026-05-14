import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ report_id: string }> },
) {
  const a = await auth();
  if (!a?.userId)
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  const token = await getClerkToken(a.getToken);
  if (!token)
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });

  const { report_id } = await params;

  // The execute endpoint requires confirm=true and lets the caller
  // mark the source. Default to "web" since this Next route is only
  // ever hit by the dashboard.
  let body: unknown = {};
  try {
    body = await request.json();
  } catch {
    body = {};
  }
  const payload = {
    cells: null,
    confirm: true,
    source: "web",
    ...(typeof body === "object" && body !== null ? body : {}),
  };

  const res = await fetch(
    getBackendUrl(
      "reports",
      `/${encodeURIComponent(report_id)}/backfill/execute`,
    ),
    {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json", ...getAuthHeaders(token) },
      body: JSON.stringify(payload),
    },
  );
  const text = await res.text();
  return new NextResponse(text, {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
}
