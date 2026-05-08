import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

export async function GET(request: Request) {
  try {
    const authObj = await auth();
    if (!authObj || !authObj.userId) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }
    const token = await getClerkToken(authObj.getToken);
    if (!token) {
      return NextResponse.json(
        { error: "Failed to get authentication token" },
        { status: 401 },
      );
    }
    const incoming = new URL(request.url);
    const params = Object.fromEntries(incoming.searchParams.entries());
    const url = getBackendUrl("experiments", "", params);
    const res = await fetch(url, {
      cache: "no-store",
      headers: getAuthHeaders(token),
    });
    const text = await res.text();
    const data = text ? JSON.parse(text) : null;
    if (!res.ok) {
      return NextResponse.json(data ?? { error: "Upstream error" }, {
        status: res.status,
      });
    }
    return NextResponse.json(data);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}

export async function POST(request: Request) {
  try {
    const authObj = await auth();
    if (!authObj || !authObj.userId) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }
    if (!["org:admin", "org:owner"].includes(authObj.orgRole ?? "")) {
      return NextResponse.json(
        { error: "Forbidden: admin privileges required" },
        { status: 403 },
      );
    }
    const token = await getClerkToken(authObj.getToken);
    if (!token) {
      return NextResponse.json(
        { error: "Failed to get authentication token" },
        { status: 401 },
      );
    }
    const incoming = new URL(request.url);
    const params = Object.fromEntries(incoming.searchParams.entries());
    const url = getBackendUrl("experiments", "", params);
    const body = await request.text();
    const res = await fetch(url, {
      method: "POST",
      cache: "no-store",
      headers: {
        ...getAuthHeaders(token),
        "content-type": "application/json",
      },
      body,
    });
    const text = await res.text();
    const data = text ? JSON.parse(text) : null;
    if (!res.ok) {
      return NextResponse.json(data ?? { error: "Upstream error" }, {
        status: res.status,
      });
    }
    return NextResponse.json(data, { status: res.status });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
