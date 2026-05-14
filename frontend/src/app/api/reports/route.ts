import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

async function proxiedToken() {
  const authObj = await auth();
  if (!authObj?.userId) return null;
  return getClerkToken(authObj.getToken);
}

export async function GET(request: NextRequest) {
  const token = await proxiedToken();
  if (!token) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const searchParams = request.nextUrl.searchParams;
  const base = getBackendUrl("reports");
  const url = searchParams.toString()
    ? `${base}?${searchParams.toString()}`
    : base;
  const res = await fetch(url, {
    cache: "no-store",
    headers: getAuthHeaders(token),
  });
  const body = await res.text();
  return new NextResponse(body, {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
}

export async function POST(request: NextRequest) {
  const token = await proxiedToken();
  if (!token) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const body = await request.text();
  const res = await fetch(getBackendUrl("reports"), {
    method: "POST",
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...getAuthHeaders(token) },
    body,
  });
  const text = await res.text();
  return new NextResponse(text, {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
}
