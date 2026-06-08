import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

export async function GET(request: NextRequest) {
  try {
    const authObj = await auth();

    if (!authObj || !authObj.userId) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const token = await getClerkToken(authObj.getToken);

    if (!token) {
      console.error("Failed to get Clerk token for user:", authObj.userId);
      return NextResponse.json(
        { error: "Failed to get authentication token" },
        { status: 401 },
      );
    }

    const searchParams = request.nextUrl.searchParams;
    const queryString = searchParams.toString();
    const baseUrl = getBackendUrl("documents");
    const url = queryString ? `${baseUrl}?${queryString}` : baseUrl;

    const res = await fetch(url, {
      cache: "no-store",
      headers: getAuthHeaders(token),
    });

    if (!res.ok) {
      const errorText = await res.text();
      console.error(`Backend error: ${res.status} - ${errorText}`);
      let payload: unknown;
      try {
        payload = JSON.parse(errorText);
      } catch {
        payload = { detail: errorText || "Failed to fetch documents" };
      }
      return NextResponse.json(payload, { status: res.status });
    }

    const data = await res.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error("API route error:", error);
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}

export async function POST(request: NextRequest) {
  try {
    const authObj = await auth();

    if (!authObj || !authObj.userId) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const token = await getClerkToken(authObj.getToken);

    if (!token) {
      console.error("Failed to get Clerk token for user:", authObj.userId);
      return NextResponse.json(
        { error: "Failed to get authentication token" },
        { status: 401 },
      );
    }

    const body = await request.json();
    const url = getBackendUrl("documents");

    const res = await fetch(url, {
      method: "POST",
      cache: "no-store",
      headers: {
        ...getAuthHeaders(token),
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });

    if (!res.ok) {
      const errorText = await res.text();
      console.error(`Backend error: ${res.status} - ${errorText}`);
      // Forward the upstream error body verbatim so the backend's `detail`
      // (e.g. a 422 ingest validation message) reaches the client.
      let payload: unknown;
      try {
        payload = JSON.parse(errorText);
      } catch {
        payload = { detail: errorText || "Failed to create document" };
      }
      return NextResponse.json(payload, { status: res.status });
    }

    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch (error) {
    console.error("API route error:", error);
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
