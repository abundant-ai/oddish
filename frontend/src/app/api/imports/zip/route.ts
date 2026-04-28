import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

// Allow arbitrarily-large multipart bodies (the backend caps each file
// at 1 GiB; this just turns off Next.js's parser-side limits).
export const runtime = "nodejs";
export const maxDuration = 600;

export async function POST(request: NextRequest) {
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

    const backendUrl = getBackendUrl("imports/zip");
    const contentType = request.headers.get("content-type");
    if (!contentType || !contentType.startsWith("multipart/form-data")) {
      return NextResponse.json(
        { error: "Expected multipart/form-data" },
        { status: 400 },
      );
    }

    // Stream the body straight through to the backend so we don't
    // buffer hundreds of megabytes in the Next.js layer.
    const res = await fetch(backendUrl, {
      method: "POST",
      headers: {
        ...getAuthHeaders(token),
        "Content-Type": contentType,
      },
      body: request.body,
      // `duplex: "half"` is required when streaming a request body
      // through fetch in Node.js / Next.js.
      duplex: "half",
    } as RequestInit & { duplex: "half" });

    const text = await res.text();
    let payload: unknown = null;
    try {
      payload = text ? JSON.parse(text) : null;
    } catch {
      payload = { error: text };
    }

    if (!res.ok) {
      const detail =
        (typeof payload === "object" && payload && "detail" in payload
          ? (payload as { detail?: string }).detail
          : null) ?? "Import failed";
      return NextResponse.json(
        { error: detail, details: text },
        { status: res.status },
      );
    }

    return NextResponse.json(payload);
  } catch (error) {
    console.error("Imports zip API route error:", error);
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
