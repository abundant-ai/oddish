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

async function forward(
  method: "GET" | "POST" | "DELETE",
  reportId: string,
): Promise<NextResponse> {
  const token = await getToken();
  if (!token) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  const res = await fetch(
    getBackendUrl("reports", `/${encodeURIComponent(reportId)}/share`),
    { method, cache: "no-store", headers: getAuthHeaders(token) },
  );
  const text = await res.text();
  return new NextResponse(text, {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
}

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ report_id: string }> },
) {
  const { report_id } = await params;
  return forward("GET", report_id);
}

export async function POST(
  _request: NextRequest,
  { params }: { params: Promise<{ report_id: string }> },
) {
  const { report_id } = await params;
  return forward("POST", report_id);
}

export async function DELETE(
  _request: NextRequest,
  { params }: { params: Promise<{ report_id: string }> },
) {
  const { report_id } = await params;
  return forward("DELETE", report_id);
}
