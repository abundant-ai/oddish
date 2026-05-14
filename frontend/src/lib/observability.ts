"use client";

/**
 * Pydantic Logfire browser tracing.
 *
 * Boots OpenTelemetry's web auto-instrumentations (fetch, document
 * load, click interactions, etc.) and ships spans to the backend
 * `/logfire-proxy/v1/traces` endpoint. The proxy attaches the
 * `LOGFIRE_TOKEN` server-side so the write token never ships to the
 * browser. The `POST /logfire-proxy/v1/traces` calls you see in
 * Logfire are exactly that — batched span uploads from the browser.
 *
 * Distributed tracing requires two prerequisites:
 *
 *   1. The browser fetch instrumentation INJECTS `traceparent` on the
 *      outbound request. By default it only does this for SAME-ORIGIN
 *      URLs, so our cross-origin API calls (Vercel → Modal) need
 *      `propagateTraceHeaderCorsUrls` set permissively (see below).
 *   2. FastAPI EXTRACTS `traceparent` and uses it as the parent of
 *      its server span. `logfire.instrument_fastapi` does this via
 *      OpenTelemetry's `OpenTelemetryMiddleware`.
 *
 * If either link is missing the browser fetch span and the FastAPI
 * span end up with different `trace_id`s and Logfire shows them as
 * unrelated traces.
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
      // Default `BatchSpanProcessor` buffers for 5s, which is fine
      // for throughput but means backend spans (children) get
      // exported by Modal long before the parent browser span
      // leaves the SDK queue. Logfire then shows the trace as
      // "missing its root" until the next flush. Tighten the
      // interval so the parent reaches Logfire within a second of
      // the child, and cap the batch size so a single tick can't
      // hold the queue for the full interval.
      batchSpanProcessorConfig: {
        scheduledDelayMillis: 1000,
        maxExportBatchSize: 64,
      },
      // Tag every browser span with the deployment provenance so a
      // preview trace is filterable down to a single PR — without
      // this only the backend + Next.js edge spans carry `oddish.pr`
      // and the browser side is anonymous across all previews.
      resourceAttributes: {
        ...(process.env.NEXT_PUBLIC_VERCEL_GIT_PULL_REQUEST_ID
          ? { "oddish.pr": process.env.NEXT_PUBLIC_VERCEL_GIT_PULL_REQUEST_ID }
          : {}),
        ...(process.env.NEXT_PUBLIC_VERCEL_GIT_COMMIT_REF
          ? { "oddish.git_branch": process.env.NEXT_PUBLIC_VERCEL_GIT_COMMIT_REF }
          : {}),
        ...(process.env.NEXT_PUBLIC_VERCEL_ENV
          ? { "oddish.vercel_env": process.env.NEXT_PUBLIC_VERCEL_ENV }
          : {}),
      },
      instrumentations: [
        getWebAutoInstrumentations({
          // Default behaviour is "same-origin only" for `traceparent`
          // injection — which silently breaks cross-service nesting
          // for our Vercel→Modal calls. Allow propagation to ANY
          // http(s) URL: the only thing that crosses the wire is the
          // `traceparent` + `tracestate` headers (32-byte trace id +
          // 16-byte span id), which are not sensitive. Keying off
          // `NEXT_PUBLIC_API_URL` like we used to is fragile — it
          // bakes the API origin into the bundle at build time and
          // any drift (preview, local override, prod swap) silently
          // disables propagation. A permissive regex avoids that
          // entire class of bug.
          "@opentelemetry/instrumentation-fetch": {
            propagateTraceHeaderCorsUrls: [/^https?:\/\//],
            clearTimingResources: true,
          },
          // Same idea for XHR — anything still using XMLHttpRequest
          // (some third-party SDKs do) should also propagate context
          // so its server span links up to the browser trace.
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

/**
 * Force-flush queued spans when the page is about to go away.
 *
 * Without this, a buffer of in-flight spans (the root browser-fetch
 * span on a navigation, a click handler's `withUserAction` span,
 * etc.) gets dropped when the user closes the tab or navigates,
 * and Logfire ends up with permanently parentless backend spans.
 *
 * We listen for both `visibilitychange → hidden` (covers tab close,
 * background, mobile-app switch) and `pagehide` (covers
 * navigation, BFCache eviction). `forceFlush` is best-effort;
 * we deliberately don't await it because the browser won't keep
 * the page alive for us.
 */
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
