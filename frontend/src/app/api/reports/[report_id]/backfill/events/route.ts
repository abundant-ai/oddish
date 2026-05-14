import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

export async function GET(
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
  const search = request.nextUrl.searchParams.toString();
  const base = getBackendUrl(
    "reports",
    `/${encodeURIComponent(report_id)}/backfill/events`,
  );
  const url = search ? `${base}?${search}` : base;

  const res = await fetch(url, {
    cache: "no-store",
    headers: getAuthHeaders(token),
  });
  const text = await res.text();
  return new NextResponse(text, {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
}
