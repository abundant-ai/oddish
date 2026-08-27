import { expect, test } from "@playwright/test";

import { proxyPublicBackendResponse } from "../src/lib/backend-response";

test("public experiment proxies preserve upstream bytes, status, and trace headers", async () => {
  const originalFetch = globalThis.fetch;
  let upstreamHeaders: Headers | undefined;
  globalThis.fetch = async (_input, init) => {
    upstreamHeaders = new Headers(init?.headers);
    return new Response('{"exact":"bytes"}\n', {
      status: 206,
      headers: {
        "Cache-Control": "public, max-age=30",
        "Content-Type": "application/x-ndjson",
        "Server-Timing": "backend_total;dur=42.0",
        traceparent: "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "x-request-id": "request-1",
      },
    });
  };

  try {
    const request = new Request(
      "https://oddish.example/api/public/experiments/token/open",
      {
        headers: {
          traceparent:
            "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01",
        },
      }
    );
    const response = await proxyPublicBackendResponse({
      request,
      path: "public/experiments/token/open",
    });

    expect(upstreamHeaders?.get("traceparent")).toBe(
      "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01"
    );
    expect(response.status).toBe(206);
    expect(response.headers.get("server-timing")).toBe(
      "backend_total;dur=42.0"
    );
    expect(response.headers.get("cache-control")).toBe("public, max-age=30");
    expect(response.headers.get("content-type")).toBe("application/x-ndjson");
    expect(response.headers.get("x-request-id")).toBe("request-1");
    expect(await response.text()).toBe('{"exact":"bytes"}\n');
  } finally {
    globalThis.fetch = originalFetch;
  }
});
