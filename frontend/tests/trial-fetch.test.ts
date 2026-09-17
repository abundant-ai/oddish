import assert from "node:assert/strict";
import test from "node:test";

import {
  clearTrialReload,
  markTrialForReload,
  trialRequestInit,
} from "../src/lib/trial-fetch.ts";
import {
  attachUpstreamCacheHeaders,
  forwardConditionalHeaders,
} from "../src/lib/proxy-cache-headers.ts";

test("a plain trial fetch lets the browser honour the backend's cache policy", () => {
  const init = trialRequestInit("/api/trials/t1");
  assert.equal(init.cache, undefined);
  assert.ok(init.signal instanceof AbortSignal);
});

test("a reload mark survives failed fetches and is spent only on success", () => {
  markTrialForReload("/api/trials/t2");
  // Two attempts (say a timeout, then SWR's retry) both bypass the cache.
  assert.equal(trialRequestInit("/api/trials/t2").cache, "reload");
  assert.equal(trialRequestInit("/api/trials/t2").cache, "reload");
  assert.equal(trialRequestInit("/api/trials/other").cache, undefined);
  clearTrialReload("/api/trials/t2");
  assert.equal(trialRequestInit("/api/trials/t2").cache, undefined);
});

test("the proxy forwards the browser's validator and the backend's cache headers", () => {
  const browserRequest = new Request("https://app.example/api/trials/t1", {
    headers: { "If-None-Match": 'W/"abc"', traceparent: "00-1-2-01" },
  });
  const forwarded = forwardConditionalHeaders(
    browserRequest,
    new Headers({ Authorization: "Bearer x" })
  );
  assert.equal(forwarded.get("if-none-match"), 'W/"abc"');
  assert.equal(forwarded.get("authorization"), "Bearer x");

  const upstream = new Response("{}", {
    headers: {
      "Cache-Control": "private, max-age=86400",
      ETag: 'W/"abc"',
      Vary: "Authorization",
      "Server-Timing": "db;dur=3",
    },
  });
  const out = attachUpstreamCacheHeaders(Response.json({}), upstream);
  assert.equal(out.headers.get("cache-control"), "private, max-age=86400");
  assert.equal(out.headers.get("etag"), 'W/"abc"');
  assert.equal(out.headers.get("vary"), "Authorization");

  const notModified = attachUpstreamCacheHeaders(
    new Response(null, { status: 304 }),
    upstream
  );
  assert.equal(notModified.status, 304);
  assert.equal(notModified.headers.get("etag"), 'W/"abc"');
});
