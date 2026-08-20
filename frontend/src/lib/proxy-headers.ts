import { joinServerTimingHeaders } from "./server-timing";

const TRACE_CONTEXT_HEADERS = ["traceparent", "tracestate", "baggage"] as const;

export function backendFetchHeaders(
  request: Request,
  headers?: HeadersInit
): Headers {
  const forwarded = new Headers(headers);
  for (const name of TRACE_CONTEXT_HEADERS) {
    const value = request.headers.get(name);
    if (value) forwarded.set(name, value);
  }
  return forwarded;
}

export function attachUpstreamServerTiming<T extends Response>(
  response: T,
  upstream: Response
): T {
  const serverTiming = joinServerTimingHeaders(
    response.headers.get("server-timing"),
    upstream.headers.get("server-timing")
  );
  if (serverTiming) response.headers.set("Server-Timing", serverTiming);
  return response;
}
