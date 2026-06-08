import { NextRequest, NextResponse } from "next/server";

// Same-origin relay for browser OpenTelemetry / Logfire trace export.
//
// The browser cannot POST spans straight to Logfire: the ingestion host
// (`logfire-api.pydantic.dev/v1/traces`) is the server-side OTLP endpoint and
// returns no `Access-Control-Allow-Origin`, so the browser blocks the
// cross-origin export at the CORS preflight — it only succeeds server-side
// (e.g. from curl or the Python backend). Logfire's own guidance is to relay
// browser telemetry through your own backend, which also keeps the write token
// off the client. The client half lives in `src/lib/observability.ts`
// (`traceUrl`), and `middleware.ts` marks this path public so unauthenticated
// pages can still flush spans.

const LOGFIRE_TRACE_URL = "https://logfire-api.pydantic.dev/v1/traces";

export async function POST(request: NextRequest): Promise<Response> {
  // The client still sends the token today; the env vars let us later drop it
  // from the browser bundle and attach it only here (server-side) with no
  // client change. Whichever is present wins.
  const authorization =
    request.headers.get("authorization") ||
    process.env.LOGFIRE_TOKEN ||
    process.env.NEXT_PUBLIC_LOGFIRE_TOKEN;

  if (!authorization) {
    // Nothing to forward with — ack so the exporter doesn't retry-storm.
    return new NextResponse(null, { status: 204 });
  }

  const body = await request.arrayBuffer();
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
