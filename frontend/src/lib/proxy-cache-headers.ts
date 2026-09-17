// The two halves of letting a browser cache a proxied backend read.
//
// Inbound, the browser's conditional-request validator has to reach the
// backend so it can answer 304 (see `backend/api/trial_cache.py`). Outbound,
// the backend's cache decision has to reach the browser: a proxy route builds
// a fresh NextResponse, which carries no Cache-Control at all, so without this
// the browser refetches even what the backend declared safe to keep.
//
// Kept free of other imports so plain `node --test` can load it.

const CONDITIONAL_HEADERS = ["if-none-match"] as const;
const CACHE_HEADERS = ["cache-control", "etag", "vary"] as const;

export function forwardConditionalHeaders(
  request: Request,
  forwarded: Headers
): Headers {
  for (const name of CONDITIONAL_HEADERS) {
    const value = request.headers.get(name);
    if (value) forwarded.set(name, value);
  }
  return forwarded;
}

export function attachUpstreamCacheHeaders<T extends Response>(
  response: T,
  upstream: Response
): T {
  for (const name of CACHE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) response.headers.set(name, value);
  }
  return response;
}
