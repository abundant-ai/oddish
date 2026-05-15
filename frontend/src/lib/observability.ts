"use client";

// Pydantic Logfire browser tracing. Spans ship to the backend's
// `/logfire-proxy/v1/traces`, which attaches `LOGFIRE_TOKEN` server-side
// so the write token never reaches the client.

import { trace, type Span, SpanStatusCode } from "@opentelemetry/api";
import { getWebAutoInstrumentations } from "@opentelemetry/auto-instrumentations-web";
import * as logfire from "@pydantic/logfire-browser";

let configured = false;

const TRACER_NAME = "oddish-frontend";

function resolveProxyUrl(apiUrl: string | undefined): string | null {
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
  // Per-PR bucket so previews don't all share one "preview" env. Keyed off
  // `NEXT_PUBLIC_VERCEL_ENV` not `NODE_ENV` — the latter is "production"
  // for PR-preview builds too.
  if (process.env.NEXT_PUBLIC_VERCEL_ENV === "production") return "production";
  const pr = process.env.NEXT_PUBLIC_VERCEL_GIT_PULL_REQUEST_ID;
  return pr ? `preview-pr-${pr}` : "preview";
}

/**
 * Idempotently configure Logfire browser tracing.
 *
 * Safe to call from React effects: subsequent calls short-circuit.
 *
 * **Opt-in only.** The browser SDK is OFF by default because every
 * batch of spans posts to the backend's ``/logfire-proxy/v1/traces``
 * route, which on Modal eats one container concurrency slot per
 * request. With aggressive 1s flush + auto-fetch instrumentation a
 * single page load can fire dozens of POSTs, contending with real
 * API traffic. Server-side Logfire (FastAPI/asyncpg auto-instrumentation)
 * keeps working independently and does NOT route through the browser
 * SDK, so disabling this does not blind the backend.
 *
 * Set ``NEXT_PUBLIC_LOGFIRE_ENABLED=true`` to opt back in for local
 * debugging or short observability sessions.
 */
export function ensureLogfireConfigured(): void {
  if (configured) return;
  if (typeof window === "undefined") return;
  if (process.env.NEXT_PUBLIC_LOGFIRE_ENABLED !== "true") return;

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
      // Tighter than the 5s SDK default so the root browser span reaches
      // Logfire before its backend children, otherwise the trace renders
      // as "missing its root" until the next flush.
      batchSpanProcessorConfig: {
        scheduledDelayMillis: 1000,
        maxExportBatchSize: 64,
      },
      resourceAttributes: {
        ...(process.env.NEXT_PUBLIC_VERCEL_GIT_PULL_REQUEST_ID
          ? { "oddish.pr": process.env.NEXT_PUBLIC_VERCEL_GIT_PULL_REQUEST_ID }
          : {}),
        ...(process.env.NEXT_PUBLIC_VERCEL_GIT_COMMIT_REF
          ? {
              "oddish.git_branch":
                process.env.NEXT_PUBLIC_VERCEL_GIT_COMMIT_REF,
            }
          : {}),
        ...(process.env.NEXT_PUBLIC_VERCEL_ENV
          ? { "oddish.vercel_env": process.env.NEXT_PUBLIC_VERCEL_ENV }
          : {}),
      },
      instrumentations: [
        getWebAutoInstrumentations({
          // Propagate `traceparent` to any http(s) URL; the same-origin
          // default silently breaks cross-service nesting for our
          // Vercel→Modal calls. Trace ids are not sensitive.
          "@opentelemetry/instrumentation-fetch": {
            propagateTraceHeaderCorsUrls: [/^https?:\/\//],
            clearTimingResources: true,
          },
          "@opentelemetry/instrumentation-xml-http-request": {
            propagateTraceHeaderCorsUrls: [/^https?:\/\//],
          },
        }),
      ],
    });
    configured = true;
    installFlushHandlers();
  } catch (err) {
    // Never let observability take down the app.
    console.warn("Logfire browser configure failed", err);
  }
}

// Force-flush on `visibilitychange → hidden` and `pagehide` so in-flight
// browser spans aren't dropped on navigation / tab close, which would
// leave their backend children parentless in Logfire. Fire-and-forget —
// the browser doesn't keep the page alive for the promise.
function installFlushHandlers(): void {
  if (typeof document === "undefined") return;

  const flush = () => {
    try {
      const provider = trace.getTracerProvider() as {
        forceFlush?: () => Promise<void>;
      };
      provider.forceFlush?.().catch(() => {
        /* swallow; flushing is best-effort on unload */
      });
    } catch {
      /* swallow */
    }
  };

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flush();
  });
  window.addEventListener("pagehide", flush);
}

/**
 * Wrap a user-meaningful action in a top-level span so the click → fetch →
 * backend → worker flow shows up as one trace instead of a bag of
 * disconnected auto-spans. Exceptions are recorded and re-thrown.
 *
 *   await withUserAction("user.cancel_trial", { trial_id }, () =>
 *     fetch(`/api/trials/${trial_id}/cancel`, { method: "POST" }),
 *   );
 */
export async function withUserAction<T>(
  name: string,
  attributesOrFn:
    | Record<string, string | number | boolean>
    | (() => Promise<T> | T),
  maybeFn?: () => Promise<T> | T,
): Promise<T> {
  const attributes = typeof attributesOrFn === "function" ? {} : attributesOrFn;
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
