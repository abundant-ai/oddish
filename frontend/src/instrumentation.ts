// Server-side OTEL via `@vercel/otel`. Patches the runtime `fetch` so
// `/api/*` route handlers propagate the incoming `traceparent` through to
// Modal — without this the Browser → Next.js → FastAPI chain breaks into
// three unrelated traces. Server-side has the write token directly, so
// spans go to Logfire over OTLP instead of round-tripping through the
// backend proxy.

import { registerOTel, OTLPHttpJsonTraceExporter } from "@vercel/otel";

export function register() {
  const token = process.env.LOGFIRE_TOKEN;
  if (!token) {
    return;
  }

  const endpoint =
    process.env.LOGFIRE_OTLP_ENDPOINT ||
    "https://logfire-api.pydantic.dev/v1/traces";

  // Per-PR Logfire environment so previews don't share one "preview" bucket.
  const environment = (() => {
    const explicit = process.env.LOGFIRE_ENVIRONMENT;
    if (explicit) return explicit;
    if (process.env.VERCEL_ENV === "production") return "prod";
    const pr = process.env.VERCEL_GIT_PULL_REQUEST_ID;
    return pr ? `preview-pr-${pr}` : "preview";
  })();

  const sha = process.env.VERCEL_GIT_COMMIT_SHA || process.env.GIT_COMMIT_SHA;

  registerOTel({
    // Distinct from the browser-side `oddish-frontend` so Logfire can tell
    // edge spans apart from browser spans on the same trace.
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
        // `@vercel/otel` defaults to Vercel-only URLs; broaden to all
        // http(s) so our outbound Modal calls keep the trace context.
        propagateContextUrls: [/^https?:\/\//],
      },
    },
    traceExporter: new OTLPHttpJsonTraceExporter({
      url: endpoint,
      headers: { Authorization: token },
    }),
  });
}
