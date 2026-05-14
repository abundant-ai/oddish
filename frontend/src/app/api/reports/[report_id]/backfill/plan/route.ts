import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ report_id: string }> },
) {
  const a = await auth();
  if (!a?.userId)
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  const token = await getClerkToken(a.getToken);
  if (!token)
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });

  const { report_id } = await params;
  const res = await fetch(
    getBackendUrl(
      "reports",
      `/${encodeURIComponent(report_id)}/backfill/plan`,
    ),
    { cache: "no-store", headers: getAuthHeaders(token) },
  );
  const text = await res.text();
  return new NextResponse(text, {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
}
