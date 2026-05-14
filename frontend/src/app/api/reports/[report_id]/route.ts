import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

async function getToken(): Promise<string | null> {
  const a = await auth();
  if (!a?.userId) return null;
  return getClerkToken(a.getToken);
}

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ report_id: string }> },
) {
  const token = await getToken();
  if (!token) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  const { report_id } = await params;
  const res = await fetch(
    getBackendUrl("reports", `/${encodeURIComponent(report_id)}`),
    { cache: "no-store", headers: getAuthHeaders(token) },
  );
  const text = await res.text();
  return new NextResponse(text, {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
}

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ report_id: string }> },
) {
  const token = await getToken();
  if (!token) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  const { report_id } = await params;
  const body = await request.text();
  const res = await fetch(
    getBackendUrl("reports", `/${encodeURIComponent(report_id)}`),
    {
      method: "PATCH",
      cache: "no-store",
      headers: { "Content-Type": "application/json", ...getAuthHeaders(token) },
      body,
    },
  );
  const text = await res.text();
  return new NextResponse(text, {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
}

export async function DELETE(
  _request: NextRequest,
  { params }: { params: Promise<{ report_id: string }> },
) {
  const token = await getToken();
  if (!token) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  const { report_id } = await params;
  const res = await fetch(
    getBackendUrl("reports", `/${encodeURIComponent(report_id)}`),
    {
      method: "DELETE",
      cache: "no-store",
      headers: getAuthHeaders(token),
    },
  );
  const text = await res.text();
  return new NextResponse(text, {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
}
