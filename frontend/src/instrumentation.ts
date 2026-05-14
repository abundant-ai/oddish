/**
 * Next.js OpenTelemetry registration for server-side runtime.
 *
 * Next.js calls `register()` once per server boot. We use Vercel's
 * official `@vercel/otel` to set up an OTLP exporter pointed at
 * Logfire's hosted endpoint, plus auto-instrumentation for `fetch`
 * so every server-side `fetch()` made by an API route or RSC carries
 * the incoming request's W3C `traceparent` to its upstream call.
 *
 * Why this exists
 * ---------------
 * The dashboard uses `/api/*` Next.js route handlers as authenticated
 * proxies to the Modal FastAPI. Without this file, the chain
 * Browser → /api/experiments/:id/tasks → Modal /tasks?...
 * loses trace context at the middle hop, because Next.js's built-in
 * `fetch` is not instrumented out of the box. FastAPI then starts a
 * fresh trace and Logfire shows the browser span and the FastAPI span
 * as unrelated. `@vercel/otel` fixes that by patching the global
 * `fetch` on the server side.
 *
 * Auth
 * ----
 * Server-side has the Logfire write token (`LOGFIRE_TOKEN`) as a
 * server-only env var on Vercel, so we ship spans direct to Logfire
 * over OTLP/HTTP — no need to route them through the backend proxy
 * (the proxy exists purely to keep the token out of the browser).
 *
 * Disabled when `LOGFIRE_TOKEN` is unset (local dev without an
 * observability account).
 */

import { registerOTel, OTLPHttpJsonTraceExporter } from "@vercel/otel";

export function register() {
  const token = process.env.LOGFIRE_TOKEN;
  if (!token) {
    return;
  }

  const endpoint =
    process.env.LOGFIRE_OTLP_ENDPOINT ||
    "https://logfire-api.pydantic.dev/v1/traces";

  // Per-deployment env so a PR's edge spans land in their own Logfire
  // environment instead of all PRs sharing a single "preview" bucket.
  // Falls through to the explicit override or the generic "preview"
  // label when no PR number is available (local dev, ad-hoc deploys).
  const environment = (() => {
    const explicit = process.env.LOGFIRE_ENVIRONMENT;
    if (explicit) return explicit;
    if (process.env.VERCEL_ENV === "production") return "prod";
    const pr = process.env.VERCEL_GIT_PULL_REQUEST_ID;
    return pr ? `preview-pr-${pr}` : "preview";
  })();

  const sha = process.env.VERCEL_GIT_COMMIT_SHA || process.env.GIT_COMMIT_SHA;

  registerOTel({
    // Distinct from the browser-side `oddish-frontend` so Logfire
    // can tell which spans came from a user's browser vs. the
    // Vercel runtime executing a route handler. Both still share a
    // trace id when wired correctly, just different `service.name`.
    // `@vercel/otel` doesn't accept `serviceVersion` directly — set
    // it on the resource via the `service.version` attribute.
    serviceName: "oddish-frontend-edge",
    attributes: {
      "deployment.environment": environment,
      ...(sha ? { "service.version": sha } : {}),
      ...(process.env.VERCEL_GIT_PULL_REQUEST_ID
        ? { "oddish.pr": process.env.VERCEL_GIT_PULL_REQUEST_ID }
        : {}),
      ...(process.env.VERCEL_GIT_COMMIT_REF
        ? { "oddish.git_branch": process.env.VERCEL_GIT_COMMIT_REF }
        : {}),
    },
    instrumentationConfig: {
      fetch: {
        // `@vercel/otel`'s fetch instrumentation defaults to
        // "propagate context ONLY to Vercel-generated URLs", which
        // means our outbound fetches from /api/* routes to Modal
        // (`*.modal.run`) ship without a `traceparent` header. That
        // makes the FastAPI request look like a fresh trace and
        // breaks Browser → Next.js route → Modal nesting.
        //
        // Open the allowlist to every http(s) URL. Trace ids /
        // span ids aren't sensitive, so leaking them to whatever
        // we fetch isn't a real concern; misrouting traces because
        // someone added a new backend host IS.
        propagateContextUrls: [/^https?:\/\//],
      },
    },
    traceExporter: new OTLPHttpJsonTraceExporter({
      url: endpoint,
      headers: {
        // Logfire accepts the raw token as the `Authorization` value
        // (no `Bearer` prefix). Same token the Modal backend uses;
        // it's a write-only key, so re-using it on the Vercel
        // runtime is fine.
        Authorization: token,
      },
    }),
  });
}
