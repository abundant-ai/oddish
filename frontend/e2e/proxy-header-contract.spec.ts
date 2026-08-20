import { expect, test } from "@playwright/test";

import {
  attachUpstreamServerTiming,
  backendFetchHeaders,
} from "../src/lib/proxy-headers";

test.describe("backend proxy header contract", () => {
  test("forwards W3C trace context without copying unrelated browser headers", () => {
    const request = new Request("https://oddish.example/api/tasks", {
      headers: {
        baggage: "account.id=123",
        cookie: "session=browser-only",
        traceparent: "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        tracestate: "vendor=value",
      },
    });

    const headers = backendFetchHeaders(request, {
      Authorization: "Bearer backend-token",
    });

    expect(headers.get("authorization")).toBe("Bearer backend-token");
    expect(headers.get("baggage")).toBe("account.id=123");
    expect(headers.get("traceparent")).toBe(
      "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    );
    expect(headers.get("tracestate")).toBe("vendor=value");
    expect(headers.has("cookie")).toBe(false);
  });

  for (const status of [200, 503]) {
    test(`joins upstream timing on a ${status} response`, () => {
      const upstream = new Response(null, {
        status,
        headers: { "Server-Timing": "backend_total;dur=42.0" },
      });
      const response = new Response(null, {
        status,
        headers: { "Server-Timing": "next_total;dur=5.0" },
      });

      expect(attachUpstreamServerTiming(response, upstream)).toBe(response);
      expect(response.headers.get("server-timing")).toBe(
        "next_total;dur=5.0, backend_total;dur=42.0"
      );
    });
  }

  test("attaches timing without consuming a streamed body", async () => {
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('{"path":"one"}\n'));
        controller.close();
      },
    });
    const upstream = new Response(stream, {
      headers: { "Server-Timing": "backend_stream;dur=8.0" },
    });
    const response = attachUpstreamServerTiming(
      new Response(upstream.body),
      upstream
    );

    expect(response.headers.get("server-timing")).toBe(
      "backend_stream;dur=8.0"
    );
    expect(await response.text()).toBe('{"path":"one"}\n');
  });
});
