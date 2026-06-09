import { NextRequest, NextResponse } from "next/server";

// Browsers can't POST spans straight to Logfire: its ingest host returns no
// CORS headers, so the cross-origin export is blocked at preflight. Relay
// same-origin instead (client half: src/lib/observability.ts). The route is
// public, so it's gated to keep it from being an open Logfire amplifier.
const LOGFIRE_TRACE_URL = "https://logfire-api.pydantic.dev/v1/traces";
const MAX_BODY_BYTES = 512 * 1024;

function isSameOrigin(request: NextRequest): boolean {
  const origin = request.headers.get("origin");
  if (!origin) return false;
  const host =
    request.headers.get("x-forwarded-host")?.split(",")[0].trim() ||
    request.headers.get("host");
  try {
    return new URL(origin).host === host;
  } catch {
    return false;
  }
}

// Enforce the cap while streaming — a chunked body with no Content-Length
// would otherwise buffer unbounded before a post-hoc byteLength check.
async function readBodyCapped(
  request: NextRequest,
  cap: number,
): Promise<Uint8Array<ArrayBuffer> | null> {
  const stream = request.body;
  if (!stream) return new Uint8Array(0);
  const reader = stream.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > cap) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const body = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return body;
}

export async function POST(request: NextRequest): Promise<Response> {
  const sameOrigin = isSameOrigin(request);
  if (request.headers.get("origin") && !sameOrigin) {
    return new NextResponse(null, { status: 403 });
  }

  // NEXT_PUBLIC_LOGFIRE_TOKEN is the browser write token (see env.example);
  // only fall back to it for same-origin callers so this isn't a token-less
  // amplifier.
  const authorization =
    request.headers.get("authorization") ||
    (sameOrigin
      ? process.env.NEXT_PUBLIC_LOGFIRE_TOKEN || process.env.LOGFIRE_TOKEN
      : undefined);
  if (!authorization) return new NextResponse(null, { status: 204 });

  if (Number(request.headers.get("content-length") || "0") > MAX_BODY_BYTES) {
    return new NextResponse(null, { status: 413 });
  }
  const body = await readBodyCapped(request, MAX_BODY_BYTES);
  if (body === null) return new NextResponse(null, { status: 413 });

  const headers: Record<string, string> = {
    Authorization: authorization,
    "Content-Type": request.headers.get("content-type") || "application/json",
  };
  const encoding = request.headers.get("content-encoding");
  if (encoding) headers["Content-Encoding"] = encoding;

  try {
    const upstream = await fetch(LOGFIRE_TRACE_URL, {
      method: "POST",
      headers,
      body,
      cache: "no-store",
    });
    return new NextResponse(await upstream.arrayBuffer(), {
      status: upstream.status,
      headers: {
        "Content-Type":
          upstream.headers.get("content-type") || "application/json",
      },
    });
  } catch {
    return new NextResponse(null, { status: 204 });
  }
}
