"use client";

import { trace, type Span, SpanStatusCode } from "@opentelemetry/api";
import { getWebAutoInstrumentations } from "@opentelemetry/auto-instrumentations-web";
import * as logfire from "@pydantic/logfire-browser";

let configured = false;

const TRACER_NAME = "oddish-frontend";
const LOGFIRE_TRACE_URL = "/api/client-traces";

function apiOriginPatterns(): RegExp[] {
  const apiUrl = process.env.NEXT_PUBLIC_API_URL;
  if (!apiUrl) return [];
  const escaped = apiUrl
    .replace(/\/+$/, "")
    .replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return [new RegExp(`^${escaped}(/|$)`)];
}

function resolveEnvironment(): string {
  const explicit = process.env.NEXT_PUBLIC_LOGFIRE_ENVIRONMENT;
  if (explicit) return explicit;
  if (process.env.NEXT_PUBLIC_VERCEL_ENV === "production") return "production";
  const pr = process.env.NEXT_PUBLIC_VERCEL_GIT_PULL_REQUEST_ID;
  return pr ? `preview-pr-${pr}` : "preview";
}

export function ensureLogfireConfigured(): void {
  if (configured) return;
  if (typeof window === "undefined") return;

  try {
    logfire.configure({
      traceUrl: LOGFIRE_TRACE_URL,
      serviceName: "oddish-frontend",
      serviceVersion:
        process.env.NEXT_PUBLIC_APP_VERSION ||
        process.env.NEXT_PUBLIC_VERCEL_GIT_COMMIT_SHA ||
        undefined,
      environment: resolveEnvironment(),
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
            // Direct API mode calls the backend origin from the browser; send
            // traceparent there too so those spans join the backend trace.
            propagateTraceHeaderCorsUrls: apiOriginPatterns(),
          },
        }),
      ],
    });
    configured = true;
    installFlushHandlers();
  } catch (err) {
    console.warn("Logfire browser configure failed", err);
  }
}

export function recordClientError(
  name: string,
  attributes: Record<string, string | number | boolean>
): void {
  console.error(`[${name}]`, attributes);
  if (!configured) return;
  const span = trace.getTracer(TRACER_NAME).startSpan(name, { attributes });
  span.setStatus({ code: SpanStatusCode.ERROR });
  span.end();
}

type FlushableProvider = { forceFlush?: () => Promise<void> };

/**
 * Push queued spans to the exporter now instead of waiting for the batch timer.
 *
 * Reaching the real provider takes one extra step that is easy to miss.
 * ``trace.getTracerProvider()`` hands back a ``ProxyTracerProvider`` — a
 * stand-in the API registers so ``getTracer`` works before any SDK loads — and
 * it forwards tracer creation but implements no ``forceFlush``. Calling
 * ``provider.forceFlush?.()`` on it therefore skips silently: optional chaining
 * finds nothing, no error is raised, and nothing is flushed. The delegate it
 * wraps is the object that can actually flush.
 *
 * Returns whether a flush was genuinely started, so callers can tell "flushed"
 * from "quietly did nothing".
 */
export function flushTelemetry(): boolean {
  try {
    const proxy = trace.getTracerProvider() as FlushableProvider & {
      getDelegate?: () => FlushableProvider;
    };
    const provider = proxy.getDelegate?.() ?? proxy;
    if (typeof provider.forceFlush !== "function") return false;
    provider.forceFlush().catch(() => {
      /* best effort; the page is usually going away */
    });
    return true;
  } catch {
    return false;
  }
}

function installFlushHandlers(): void {
  if (typeof document === "undefined") return;

  const flush = () => {
    flushTelemetry();
  };

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flush();
  });
  window.addEventListener("pagehide", flush);
}

export async function withUserAction<T>(
  name: string,
  attributesOrFn:
    | Record<string, string | number | boolean>
    | (() => Promise<T> | T),
  maybeFn?: () => Promise<T> | T
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
