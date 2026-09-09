import assert from "node:assert/strict";
import test from "node:test";
import { trace } from "@opentelemetry/api";

import { flushTelemetry } from "../src/lib/telemetry-flush.ts";

type CountingProvider = {
  getTracer: () => unknown;
  forceFlush: () => Promise<void>;
  calls: number;
};

/**
 * Register a provider as THE global one.
 *
 * `setGlobalTracerProvider` refuses to overwrite an existing registration --
 * it warns and keeps the incumbent -- so without the reset every test after
 * the first would silently assert against the first test's provider.
 */
function register(provider: unknown): void {
  trace.disable();
  trace.setGlobalTracerProvider(
    provider as Parameters<typeof trace.setGlobalTracerProvider>[0]
  );
}

function countingProvider(): CountingProvider {
  const provider = {
    calls: 0,
    getTracer: () => ({
      startSpan: () => ({
        setAttribute() {},
        setStatus() {},
        end() {},
        isRecording: () => true,
      }),
    }),
    forceFlush: async () => {
      provider.calls += 1;
    },
  };
  return provider;
}

test("the global tracer provider cannot be flushed directly", () => {
  // The defect this guards against. `trace.getTracerProvider()` returns a
  // ProxyTracerProvider and KEEPS returning it after a real provider is
  // registered. It has no forceFlush, so the natural `provider.forceFlush?.()`
  // resolves to undefined and skips with no error and no flush -- a silent gap
  // in exactly the abandonment records the hidden-page handler exists to save.
  const provider = countingProvider();
  register(provider);

  const proxy = trace.getTracerProvider() as {
    forceFlush?: () => Promise<void>;
  };
  assert.equal(
    typeof proxy.forceFlush,
    "undefined",
    "the proxy is expected to expose no forceFlush"
  );

  proxy.forceFlush?.();
  assert.equal(provider.calls, 0, "the naive call must reach nothing");
});

test("flushTelemetry reaches the registered provider", () => {
  const provider = countingProvider();
  register(provider);

  assert.equal(flushTelemetry(), true, "a flush should have been started");
  assert.equal(provider.calls, 1, "the provider should have been flushed once");
});

test("each call flushes again", () => {
  const provider = countingProvider();
  register(provider);

  flushTelemetry();
  flushTelemetry();
  assert.equal(provider.calls, 2);
});

test("reports false when the provider cannot flush", () => {
  // No SDK registered, or one without forceFlush: the caller is told nothing
  // happened rather than being left to assume it did.
  trace.disable();
  assert.equal(flushTelemetry(), false);
});

test("a throwing provider is reported as not flushed", () => {
  const provider = {
    getTracer: () => ({}),
    forceFlush: () => {
      throw new Error("exporter is gone");
    },
  };
  register(provider);

  assert.equal(flushTelemetry(), false);
});

test("a rejected flush does not throw at the call site", () => {
  // The page is usually going away; a failed export must not take the handler
  // down with it and leave the remaining opens unsettled.
  const provider = {
    getTracer: () => ({}),
    forceFlush: () => Promise.reject(new Error("network down")),
  };
  register(provider);

  assert.equal(flushTelemetry(), true);
});
