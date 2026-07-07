import { NextRequest, NextResponse } from "next/server";

// Same-origin proxy for browser OTLP spans. Logfire's API has no CORS
// allowance for browser origins, and proxying keeps the write token out of
// the client bundle. Public route (see middleware.ts): spans also flow from
// public share/dataset pages and from pagehide flushes.
const LOGFIRE_TRACE_URL =
  process.env.LOGFIRE_OTLP_ENDPOINT ||
  "https://logfire-api.pydantic.dev/v1/traces";
const MAX_BODY_BYTES = 1024 * 1024;

async function readBodyBounded(
  request: NextRequest,
): Promise<ArrayBuffer | null> {
  const reader = request.body?.getReader();
  if (!reader) return new ArrayBuffer(0);
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > MAX_BODY_BYTES) {
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
  return body.buffer;
}

export async function POST(request: NextRequest) {
  const token =
    process.env.LOGFIRE_BROWSER_TOKEN || process.env.LOGFIRE_TOKEN;
  if (!token) {
    return new NextResponse(null, { status: 204 });
  }

  const origin = request.headers.get("origin");
  if (origin && origin !== request.nextUrl.origin) {
    return NextResponse.json({ error: "Forbidden" }, { status: 403 });
  }

  const contentLength = Number(request.headers.get("content-length") ?? 0);
  if (Number.isFinite(contentLength) && contentLength > MAX_BODY_BYTES) {
    return NextResponse.json({ error: "Payload too large" }, { status: 413 });
  }
  // Bounded stream read: chunked/length-less bodies must not buffer past the
  // cap before rejection — this is a public unauthenticated endpoint.
  const body = await readBodyBounded(request);
  if (body === null) {
    return NextResponse.json({ error: "Payload too large" }, { status: 413 });
  }

  try {
    const upstream = await fetch(LOGFIRE_TRACE_URL, {
      method: "POST",
      headers: {
        Authorization: token,
        "Content-Type":
          request.headers.get("content-type") ?? "application/json",
      },
      body,
    });
    return new NextResponse(null, {
      status: upstream.ok ? 204 : upstream.status,
    });
  } catch {
    return new NextResponse(null, { status: 502 });
  }
}
