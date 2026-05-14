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

import { trace, type Span, SpanStatusCode } from "@opentelemetry/api";
import { getWebAutoInstrumentations } from "@opentelemetry/auto-instrumentations-web";
import * as logfire from "@pydantic/logfire-browser";

let configured = false;

const TRACER_NAME = "oddish-frontend";

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
  // Two buckets only: `prod` for the canonical production deploy
  // (opt in via `NEXT_PUBLIC_VERCEL_ENV=production` or the explicit
  // override above), `preview` for everything else — PR previews,
  // local dev, ad-hoc deploys. We deliberately do NOT key off
  // `NODE_ENV=production` because PR-preview builds set it too.
  return process.env.NEXT_PUBLIC_VERCEL_ENV === "production"
    ? "prod"
    : "preview";
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

/**
 * Wrap a user-meaningful action in a top-level span.
 *
 * Without this, every UI flow shows up in Logfire as a bag of
 * disconnected auto-spans — a `click`, three `fetch`es, a re-render
 * — with no shared root. Wrap the click handler (or the SWR mutate
 * call, or the form submit) with `withUserAction("user.create_run")`
 * and every fetch / DB query / worker job that gets pulled in nests
 * neatly underneath that named root, so an observer can see the
 * whole flow on a single trace.
 *
 * Attributes are flattened onto the span as `key=value`. Exceptions
 * are recorded and re-thrown; the span is always closed.
 *
 * Usage:
 *   await withUserAction("user.cancel_trial", { trial_id }, () =>
 *     fetch(`/api/trials/${trial_id}/cancel`, { method: "POST" }),
 *   );
 */
export async function withUserAction<T>(
  name: string,
  attributesOrFn: Record<string, string | number | boolean> | (() => Promise<T> | T),
  maybeFn?: () => Promise<T> | T,
): Promise<T> {
  const attributes =
    typeof attributesOrFn === "function" ? {} : attributesOrFn;
  const fn = typeof attributesOrFn === "function" ? attributesOrFn : maybeFn!;

  if (!configured) {
    return await fn();
  }

  const tracer = trace.getTracer(TRACER_NAME);
  return await tracer.startActiveSpan(name, async (span: Span) => {
    for (const [k, v] of Object.entries(attributes)) {
      span.setAttribute(k, v);
    }
    try {
      const result = await fn();
      span.setStatus({ code: SpanStatusCode.OK });
      return result;
    } catch (err) {
      span.recordException(err as Error);
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: err instanceof Error ? err.message : String(err),
      });
      throw err;
    } finally {
      span.end();
    }
  });
}
