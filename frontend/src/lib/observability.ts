"use client";

import { trace, type Span, SpanStatusCode } from "@opentelemetry/api";
import { getWebAutoInstrumentations } from "@opentelemetry/auto-instrumentations-web";
import * as logfire from "@pydantic/logfire-browser";

let configured = false;

const TRACER_NAME = "oddish-frontend";
const LOGFIRE_TRACE_URL = "/api/client-traces";

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

export async function withUserAction<T>(
  name: string,
  attributesOrFn:
    Record<string, string | number | boolean> | (() => Promise<T> | T),
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
