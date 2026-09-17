import assert from "node:assert/strict";
import test from "node:test";

import {
  attachUpstreamCacheHeaders,
  forwardConditionalHeaders,
} from "../src/lib/proxy-cache-headers.ts";

test("the proxy forwards the browser's validator and the backend's cache headers", () => {
  const browserRequest = new Request("https://app.example/api/trials/t1", {
    headers: { "If-None-Match": 'W/"abc"' },
  });
  const forwarded = forwardConditionalHeaders(
    browserRequest,
    new Headers({ Authorization: "Bearer x" })
  );
  assert.equal(forwarded.get("if-none-match"), 'W/"abc"');
  assert.equal(forwarded.get("authorization"), "Bearer x");

  const upstream = new Response("{}", {
    headers: {
      "Cache-Control": "private, no-cache",
      ETag: 'W/"abc"',
      Vary: "Authorization",
    },
  });
  const out = attachUpstreamCacheHeaders(Response.json({}), upstream);
  assert.equal(out.headers.get("cache-control"), "private, no-cache");
  assert.equal(out.headers.get("etag"), 'W/"abc"');
  assert.equal(out.headers.get("vary"), "Authorization");

  const notModified = attachUpstreamCacheHeaders(
    new Response(null, { status: 304 }),
    upstream
  );
  assert.equal(notModified.status, 304);
  assert.equal(notModified.headers.get("etag"), 'W/"abc"');
});
