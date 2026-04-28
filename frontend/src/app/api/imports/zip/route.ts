import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

// Use the Node runtime so we can hand the multipart body to the backend
// without Next.js trying to parse it. `maxDuration` covers slow trial
// imports (the per-trial fan-out can hit a few minutes).
export const runtime = "nodejs";
export const maxDuration = 600;
// Disable Next.js's automatic body parser; we forward the raw bytes.
export const dynamic = "force-dynamic";

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

    const contentType = request.headers.get("content-type");
    if (!contentType || !contentType.startsWith("multipart/form-data")) {
      return NextResponse.json(
        { error: "Expected multipart/form-data" },
        { status: 400 },
      );
    }

    // Buffer the body before re-issuing the request. Streaming via
    // `body: request.body, duplex: "half"` works in pure Node.js but
    // tripped `TypeError: fetch failed` under the Next.js runtime --
    // a buffered round-trip is the simple-and-it-just-works path,
    // and the backend already caps each file at 1 GiB.
    const body = await request.arrayBuffer();

    const backendUrl = getBackendUrl("imports/zip");
    const res = await fetch(backendUrl, {
      method: "POST",
      headers: {
        ...getAuthHeaders(token),
        "Content-Type": contentType,
        "Content-Length": String(body.byteLength),
      },
      body,
    });

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
          : null) ?? `Backend returned ${res.status}`;
      console.error(
        `[imports/zip] Backend error ${res.status}: ${text.slice(0, 500)}`,
      );
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
