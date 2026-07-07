import type { NextRequest } from "next/server";

const LOGFIRE_TRACE_URL =
  process.env.LOGFIRE_OTLP_ENDPOINT ||
  "https://logfire-api.pydantic.dev/v1/traces";
const MAX_BODY_BYTES = 1024 * 1024;

async function readBodyBounded(request: NextRequest) {
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
  chunks.reduce((offset, chunk) => {
    body.set(chunk, offset);
    return offset + chunk.byteLength;
  }, 0);
  return body.buffer;
}

export async function POST(request: NextRequest) {
  const token = process.env.LOGFIRE_BROWSER_TOKEN || process.env.LOGFIRE_TOKEN;
  if (!token) return new Response(null, { status: 204 });

  const origin = request.headers.get("origin");
  if (origin && origin !== request.nextUrl.origin) {
    return Response.json({ error: "Forbidden" }, { status: 403 });
  }

  const contentLength = Number(request.headers.get("content-length") ?? 0);
  if (Number.isFinite(contentLength) && contentLength > MAX_BODY_BYTES) {
    return Response.json({ error: "Payload too large" }, { status: 413 });
  }

  // Public endpoint: reject chunked bodies as soon as they cross the cap.
  const body = await readBodyBounded(request);
  if (body === null) {
    return Response.json({ error: "Payload too large" }, { status: 413 });
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
    return new Response(null, { status: upstream.ok ? 204 : upstream.status });
  } catch {
    return new Response(null, { status: 502 });
  }
}
