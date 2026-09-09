import assert from "node:assert/strict";
import test from "node:test";

import { apiFetch, directApiEnabled, resolveApiUrl } from "../src/lib/api.ts";

const direct = {
  NEXT_PUBLIC_API_DIRECT: "1",
  NEXT_PUBLIC_API_URL: "https://api.example.test/",
};

test("direct mode needs both the flag and an API URL", () => {
  assert.equal(directApiEnabled({}), false);
  assert.equal(directApiEnabled({ NEXT_PUBLIC_API_DIRECT: "1" }), false);
  assert.equal(
    directApiEnabled({ NEXT_PUBLIC_API_URL: "https://api.example.test" }),
    false
  );
  assert.equal(directApiEnabled(direct), true);
  assert.equal(
    directApiEnabled({ ...direct, NEXT_PUBLIC_API_DIRECT: "true" }),
    true
  );
  assert.equal(
    directApiEnabled({ ...direct, NEXT_PUBLIC_API_DIRECT: "0" }),
    false
  );
});

test("proxy paths map onto the backend, query string intact", () => {
  const resolved = resolveApiUrl(
    "/api/tasks/task-1/open?version_id=version-2",
    {
      env: direct,
      inBrowser: true,
    }
  );
  assert.deepEqual(resolved, {
    url: "https://api.example.test/tasks/task-1/open?version_id=version-2",
    direct: true,
    public: false,
  });
});

test("task browse retains the proxy's display-filter translation", () => {
  const input =
    "/api/tasks/browse?q=author%3Akyle%20tag%3Areview&created_within=7d";
  assert.deepEqual(resolveApiUrl(input, { env: direct, inBrowser: true }), {
    url: input,
    direct: false,
    public: false,
  });
});

test("experiment links lose only Next's extra encoding layer", () => {
  const id = "experiment with spaces%";
  const input = `/api/experiments/${encodeURIComponent(encodeURIComponent(id))}/open?limit=10`;
  assert.equal(
    resolveApiUrl(input, { env: direct, inBrowser: true }).url,
    `https://api.example.test/experiments/${encodeURIComponent(id)}/open?limit=10`
  );
});

test("the five renamed proxies are rewritten, not mirrored", () => {
  const cases: Array<[string, string]> = [
    ["/api/settings/account", "/users/me"],
    ["/api/settings/api-keys", "/api-keys"],
    ["/api/settings/api-keys/key-1", "/api-keys/key-1"],
    ["/api/settings/api-keys/permissions", "/api-keys/permissions"],
    ["/api/settings/byok/keys/anthropic", "/byok/keys/anthropic"],
    ["/api/settings/notifications", "/users/me/alert-preferences"],
    [
      "/api/admin/users/u-1/costs?window=7d",
      "/admin/costs/users/u-1?window=7d",
    ],
  ];
  for (const [input, expected] of cases) {
    const resolved = resolveApiUrl(input, { env: direct, inBrowser: true });
    assert.equal(resolved.url, `https://api.example.test${expected}`, input);
    assert.equal(resolved.direct, true, input);
  }
});

test("relays with server-side logic keep the proxy", () => {
  for (const input of [
    "/api/client-traces",
    "/api/imports/zip",
    "/api/imports/zip?x=1",
  ]) {
    assert.deepEqual(resolveApiUrl(input, { env: direct, inBrowser: true }), {
      url: input,
      direct: false,
      public: false,
    });
  }
});

test("share-page reads go direct without a token", () => {
  const resolved = resolveApiUrl("/api/public/experiments/tok/open", {
    env: direct,
    inBrowser: true,
  });
  assert.equal(resolved.public, true);
  assert.equal(
    resolved.url,
    "https://api.example.test/public/experiments/tok/open"
  );
});

test("flag off, server side, or a non-api URL leaves the request alone", () => {
  assert.equal(
    resolveApiUrl("/api/tasks", { env: {}, inBrowser: true }).direct,
    false
  );
  assert.equal(
    resolveApiUrl("/api/tasks", { env: direct, inBrowser: false }).direct,
    false
  );
  assert.equal(
    resolveApiUrl("https://elsewhere.test/api/tasks", {
      env: direct,
      inBrowser: true,
    }).direct,
    false
  );
});

async function withDirectBrowser<T>(
  clerk: unknown,
  run: () => Promise<T>
): Promise<T> {
  const g = globalThis as { window?: unknown; Clerk?: unknown };
  const saved = {
    window: g.window,
    Clerk: g.Clerk,
    flag: process.env.NEXT_PUBLIC_API_DIRECT,
    url: process.env.NEXT_PUBLIC_API_URL,
    fetch: globalThis.fetch,
  };
  g.window = {};
  g.Clerk = clerk;
  process.env.NEXT_PUBLIC_API_DIRECT = "1";
  process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
  try {
    return await run();
  } finally {
    g.window = saved.window;
    g.Clerk = saved.Clerk;
    process.env.NEXT_PUBLIC_API_DIRECT = saved.flag;
    process.env.NEXT_PUBLIC_API_URL = saved.url;
    globalThis.fetch = saved.fetch;
  }
}

test("apiFetch sends the Clerk token straight to the backend", async () => {
  const clerk = {
    loaded: true,
    session: { getToken: async () => "jwt-123" },
  };
  await withDirectBrowser(clerk, async () => {
    const calls: Array<{ url: string; init: RequestInit | undefined }> = [];
    globalThis.fetch = (async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      return new Response("{}", { status: 200 });
    }) as typeof fetch;

    await apiFetch("/api/tags", { method: "POST", body: "{}" });

    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, "https://api.example.test/tags");
    assert.equal(calls[0].init?.method, "POST");
    assert.equal(calls[0].init?.credentials, "omit");
    assert.equal(
      new Headers(calls[0].init?.headers).get("authorization"),
      "Bearer jwt-123"
    );
  });
});

test("apiFetch keeps the proxy while Clerk is still loading", async () => {
  await withDirectBrowser({ loaded: false }, async () => {
    const calls: string[] = [];
    globalThis.fetch = (async (url: string) => {
      calls.push(url);
      return new Response("{}", { status: 200 });
    }) as typeof fetch;

    await apiFetch("/api/tags");

    assert.deepEqual(calls, ["/api/tags"]);
  });
});
