"use client";

import { trace, type Span, SpanStatusCode } from "@opentelemetry/api";
import { getWebAutoInstrumentations } from "@opentelemetry/auto-instrumentations-web";
import * as logfire from "@pydantic/logfire-browser";

let configured = false;

const TRACER_NAME = "oddish-frontend";

// Browser spans are relayed through a same-origin proxy
// (`app/api/telemetry/traces`) rather than posted straight to Logfire:
// Logfire's ingestion host returns no `Access-Control-Allow-Origin`, so a
// direct cross-origin export is blocked by the browser at the CORS preflight
// (it only works server-side, e.g. from curl). See that route for details.
const LOGFIRE_TRACE_PATH = "/api/telemetry/traces";

// Never instrument the export request itself. The OTLP exporter sends spans
// via `fetch`, and `instrumentation-fetch` would otherwise wrap that POST in
// a fresh span — which then gets exported, spawning another span, a
// self-perpetuating ~1 req/s/tab heartbeat against our own relay. Excluding
// the path breaks the loop. (XHR is covered too, belt-and-suspenders.)
const TRACE_EXPORT_IGNORE_URLS = [/\/api\/telemetry\/traces(?:\?|$)/];

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

export function ensureLogfireConfigured(): void {
  if (configured) return;
  if (typeof window === "undefined") return;

  const token = process.env.NEXT_PUBLIC_LOGFIRE_TOKEN;
  if (!token) return;

  try {
    logfire.configure({
      // Absolute, same-origin URL so the export never crosses origins (no
      // CORS preflight). `window` is guaranteed defined by the guard above.
      traceUrl: `${window.location.origin}${LOGFIRE_TRACE_PATH}`,
      traceExporterHeaders: () => ({ Authorization: token }),
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
          "@opentelemetry/instrumentation-fetch": {
            clearTimingResources: true,
            ignoreUrls: TRACE_EXPORT_IGNORE_URLS,
          },
          "@opentelemetry/instrumentation-xml-http-request": {
            ignoreUrls: TRACE_EXPORT_IGNORE_URLS,
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
