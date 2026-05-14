"use client";

/**
 * Pydantic Logfire browser tracing.
 *
 * Boots OpenTelemetry's web auto-instrumentations (fetch, document
 * load, click interactions, etc.) and ships spans to the backend
 * `/logfire-proxy/v1/traces` endpoint. The proxy attaches the
 * `LOGFIRE_TOKEN` server-side so the write token never ships to the
 * browser.
 *
 * Distributed tracing works automatically: the fetch instrumentation
 * injects W3C `traceparent` headers on outbound requests, which
 * FastAPI picks up via `logfire.instrument_fastapi`, so a click in
 * the dashboard → fetch → FastAPI handler → SQLAlchemy query
 * collapses into a single trace.
 */

import { getWebAutoInstrumentations } from "@opentelemetry/auto-instrumentations-web";
import * as logfire from "@pydantic/logfire-browser";

let configured = false;

function resolveProxyUrl(apiUrl: string | undefined): string | null {
  // We point the SDK at the backend's `/logfire-proxy/v1/traces`. The
  // backend mounts the proxy only when LOGFIRE_TOKEN is set, so a
  // missing token simply produces 404s rather than leaking spans.
  if (!apiUrl) return null;
  try {
    const url = new URL(apiUrl);
    url.pathname = url.pathname.replace(/\/$/, "") + "/logfire-proxy/v1/traces";
    url.search = "";
    return url.toString();
  } catch {
    return null;
  }
}

function resolveEnvironment(): string {
  const explicit = process.env.NEXT_PUBLIC_LOGFIRE_ENVIRONMENT;
  if (explicit) return explicit;
  const vercel = process.env.NEXT_PUBLIC_VERCEL_ENV;
  if (vercel === "production") return "production";
  if (vercel === "preview") return "preview";
  if (vercel === "development") return "development";
  if (process.env.NODE_ENV === "production") return "production";
  return "development";
}

/**
 * Idempotently configure Logfire browser tracing.
 *
 * Safe to call from React effects: subsequent calls short-circuit.
 * Honours an explicit `NEXT_PUBLIC_LOGFIRE_ENABLED=false` opt-out so
 * self-hosters running without an observability stack can skip it
 * entirely.
 */
export function ensureLogfireConfigured(): void {
  if (configured) return;
  if (typeof window === "undefined") return;
  if (process.env.NEXT_PUBLIC_LOGFIRE_ENABLED === "false") return;

  const proxyUrl = resolveProxyUrl(process.env.NEXT_PUBLIC_API_URL);
  if (!proxyUrl) return;

  try {
    logfire.configure({
      traceUrl: proxyUrl,
      serviceName: "oddish-frontend",
      serviceVersion:
        process.env.NEXT_PUBLIC_APP_VERSION ||
        process.env.NEXT_PUBLIC_VERCEL_GIT_COMMIT_SHA ||
        undefined,
      environment: resolveEnvironment(),
      instrumentations: [
        getWebAutoInstrumentations({
          // Default fetch instrumentation only propagates traceparent
          // to same-origin URLs. Our API lives on a different origin
          // (Modal / Railway), so we explicitly allow it.
          "@opentelemetry/instrumentation-fetch": {
            propagateTraceHeaderCorsUrls: [
              new RegExp(
                (process.env.NEXT_PUBLIC_API_URL || "").replace(
                  /[.*+?^${}()|[\]\\]/g,
                  "\\$&",
                ) || "^$",
              ),
            ],
            clearTimingResources: true,
          },
          // Skip the noisier built-ins; keep page load + user
          // interaction + fetch which is what we actually want for
          // full-stack traces.
          "@opentelemetry/instrumentation-xml-http-request": { enabled: false },
        }),
      ],
    });
    configured = true;
  } catch (err) {
    // Never let observability take down the app.
    console.warn("Logfire browser configure failed", err);
  }
}
