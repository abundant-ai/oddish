import { trace } from "@opentelemetry/api";

/**
 * Push queued spans to the exporter now instead of waiting for the batch timer.
 *
 * Deliberately its own module, depending on nothing but the OpenTelemetry API.
 * The obvious home is ``lib/observability.ts``, but that pulls in the Logfire
 * browser SDK and the web auto-instrumentations at import time, which makes
 * this one small decision untestable outside a browser — and it is a decision
 * worth testing, because getting it wrong fails silently.
 *
 * The subtlety: ``trace.getTracerProvider()`` does NOT return the SDK's
 * provider. It returns a ``ProxyTracerProvider`` — the stand-in the API
 * registers so ``getTracer`` works before any SDK loads — and it keeps
 * returning that proxy even after a real provider is registered. The proxy
 * forwards tracer creation but implements no ``forceFlush``, so the natural
 * ``provider.forceFlush?.()`` resolves to ``undefined`` and skips without
 * error: no flush, no exception, nothing to notice. ``getDelegate()`` is the
 * step across to the object that can actually flush.
 *
 * Returns whether a flush was genuinely started, so a caller can distinguish
 * "flushed" from "quietly did nothing" — and so a regression to the silent
 * form is a failing assertion rather than an invisible gap in the data.
 */
export function flushTelemetry(): boolean {
  try {
    const proxy = trace.getTracerProvider() as {
      forceFlush?: () => Promise<void>;
      getDelegate?: () => { forceFlush?: () => Promise<void> };
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
