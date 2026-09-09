import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";
import * as timing from "../src/lib/server-timing.ts";

const code = ts.transpileModule(
  readFileSync(
    new URL("../src/lib/backend-response.ts", import.meta.url),
    "utf8"
  ),
  { compilerOptions: { module: ts.ModuleKind.CommonJS } }
).outputText;

function proxy(
  fetch: typeof globalThis.fetch,
  token: string | null = "test-token"
) {
  const exports: { proxyBackendJson?: (args: object) => Promise<Response> } =
    {};
  const modules: Record<string, unknown> = {
    "next/server": { NextResponse: Response },
    "@clerk/nextjs/server": {
      auth: async () => ({ getToken: async () => token }),
    },
    "./backend-config": {
      getClerkToken: (getToken: () => Promise<string | null>) => getToken(),
      getBackendUrl: (path: string) => `https://backend.example${path}`,
      getAuthHeaders: (value: string) => ({ Authorization: `Bearer ${value}` }),
    },
    "./proxy-headers": {
      backendFetchHeaders: (request: Request, headers: HeadersInit) => {
        const forwarded = new Headers(headers);
        const trace = request.headers.get("traceparent");
        if (trace) forwarded.set("traceparent", trace);
        return forwarded;
      },
    },
    "./server-timing": timing,
  };
  runInNewContext(code, {
    exports,
    fetch,
    performance,
    Response,
    require: (name: string) => {
      assert.ok(name in modules, `Unexpected import: ${name}`);
      return modules[name];
    },
  });
  return exports.proxyBackendJson!;
}

function assertTiming(response: Response, names: string[]) {
  const header = response.headers.get("server-timing")!;
  for (const name of names) assert.match(header, new RegExp(`${name};dur=\\d`));
  assert.equal(response.headers.get("cache-control"), "no-store");
}

for (const status of [200, 403, 503]) {
  test(`streams status ${status} without waiting for the body; keeps backend and proxy timings`, async () => {
    let controller!: ReadableStreamDefaultController;
    const body = new ReadableStream({
      start(value) {
        controller = value;
      },
    });
    const request = new Request("https://app.example/api/tasks/t/open", {
      headers: { traceparent: "trace-context" },
    });
    const run = proxy(async (url, init) => {
      assert.equal(url, "https://backend.example/tasks/t/open?version_id=t-v2");
      assert.equal(
        new Headers(init?.headers).get("authorization"),
        "Bearer test-token"
      );
      assert.equal(
        new Headers(init?.headers).get("traceparent"),
        "trace-context"
      );
      assert.equal(init?.signal, request.signal);
      return new Response(body, {
        status,
        headers: { "Server-Timing": "db_sql;dur=12" },
      });
    });
    const response = await run({
      request,
      path: "/tasks/t/open?version_id=t-v2",
      stream: true,
      signal: request.signal,
    });
    assert.equal(response.status, status);
    assertTiming(response, [
      "next_auth",
      "next_token",
      "next_upstream",
      "next_total",
      "db_sql",
    ]);
    controller.enqueue(new TextEncoder().encode("body arrived later"));
    controller.close();
    assert.equal(await response.text(), "body arrived later");
  });
}

test("buffered experiment responses measure JSON reading and retain backend errors", async () => {
  const run = proxy(async () =>
    Response.json({ detail: "not found" }, { status: 404 })
  );
  const response = await run({
    request: new Request("https://app.example/api/experiments/e/open"),
    path: "/experiments/e/open",
  });
  assert.equal(response.status, 404);
  assert.deepEqual(await response.json(), { detail: "not found" });
  assertTiming(response, ["next_json", "next_total"]);
});

test("missing tokens never reach the backend and still report auth timing", async () => {
  const run = proxy(async () => {
    throw new Error("must not fetch");
  }, null);
  const response = await run({
    request: new Request("https://app.example/api/tasks/t/open"),
    path: "/tasks/t/open",
  });
  assert.equal(response.status, 401);
  assertTiming(response, ["next_auth", "next_token", "next_total"]);
});

test("transport errors keep the failed upstream duration", async () => {
  const run = proxy(async () => {
    throw new Error("connection failed");
  });
  const response = await run({
    request: new Request("https://app.example/api/tasks/t/open"),
    path: "/tasks/t/open",
  });
  assert.equal(response.status, 503);
  assertTiming(response, ["next_upstream", "next_total"]);
});
