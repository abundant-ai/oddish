import { NextRequest, NextResponse } from "next/server";

// Same-origin relay for browser OpenTelemetry / Logfire trace export.
//
// The browser cannot POST spans straight to Logfire: the ingestion host
// (`logfire-api.pydantic.dev/v1/traces`) is the server-side OTLP endpoint and
// returns no `Access-Control-Allow-Origin`, so the browser blocks the
// cross-origin export at the CORS preflight — it only succeeds server-side
// (e.g. from curl or the Python backend). Logfire's own guidance (and the
// `@pydantic/logfire-browser` typedoc for `traceUrl`: "The URL of your trace
// exporter proxy endpoint") is to relay browser telemetry through your own
// backend, which also keeps the write token off the client. The client half
// lives in `src/lib/observability.ts` (`traceUrl`), and `middleware.ts` marks
// this path public so unauthenticated pages can still flush spans.
//
// This route is public, so it's hardened against being turned into an open
// amplifier for our Logfire project / serverless functions: requests must be
// same-origin, the body is size-capped while streaming (the app ships
// `output: "standalone"`, so self-hosted deployments have no platform body
// cap), and the server-side token fallback is only attached to same-origin
// callers (so the relay can't be driven token-lessly from elsewhere). A
// non-browser client can still spoof the Origin header; per-IP rate limiting
// (needs a shared store like Vercel KV) is the remaining mitigation and is
// left as a follow-up.

const LOGFIRE_TRACE_URL = "https://logfire-api.pydantic.dev/v1/traces";

// OTLP batches here are tiny (BatchSpanProcessor maxExportBatchSize 64);
// anything materially larger isn't our exporter.
const MAX_BODY_BYTES = 512 * 1024;

function isSameOrigin(request: NextRequest): boolean {
  const origin = request.headers.get("origin");
  if (!origin) return false;
  // Prefer the forwarded host so self-hosted deployments behind a reverse
  // proxy that rewrites Host still match the browser-visible origin.
  const forwarded = request.headers.get("x-forwarded-host");
  const host = forwarded
    ? forwarded.split(",")[0].trim()
    : request.headers.get("host");
  try {
    return new URL(origin).host === host;
  } catch {
    return false;
  }
}

// Read the body with a hard cap enforced *while* streaming. Buffering first
// and checking `byteLength` after would let a chunked request without a
// Content-Length occupy unbounded memory before being rejected.
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

  // A present Origin must match our host. The browser SDK posts same-origin,
  // so legit traffic always passes; this rejects cross-site browsers aimed at
  // our relay.
  if (request.headers.get("origin") && !sameOrigin) {
    return new NextResponse(null, { status: 403 });
  }

  // The client still sends its token today; the env fallback lets us later
  // drop it from the browser bundle with no client change. Per env.example,
  // NEXT_PUBLIC_LOGFIRE_TOKEN is the browser-facing write-only token and
  // LOGFIRE_TOKEN is the server SDK's — prefer the browser one for relayed
  // browser spans. Restricted to same-origin callers so the relay can't be
  // used as a token-less amplifier from outside the app.
  const envToken =
    process.env.NEXT_PUBLIC_LOGFIRE_TOKEN || process.env.LOGFIRE_TOKEN;
  const authorization =
    request.headers.get("authorization") || (sameOrigin ? envToken : undefined);
  if (!authorization) {
    // Nothing to forward with — ack so the exporter doesn't retry-storm.
    return new NextResponse(null, { status: 204 });
  }

  // Cheap early reject when the client declares a size; the streamed read
  // below enforces the cap regardless.
  const declaredLength = Number(request.headers.get("content-length") || "0");
  if (declaredLength > MAX_BODY_BYTES) {
    return new NextResponse(null, { status: 413 });
  }
  const body = await readBodyCapped(request, MAX_BODY_BYTES);
  if (body === null) {
    return new NextResponse(null, { status: 413 });
  }

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
    const payload = await upstream.arrayBuffer();
    return new NextResponse(payload, {
      status: upstream.status,
      headers: {
        "Content-Type":
          upstream.headers.get("content-type") || "application/json",
      },
    });
  } catch {
    // Telemetry must never surface as an app-visible failure.
    return new NextResponse(null, { status: 204 });
  }
}
