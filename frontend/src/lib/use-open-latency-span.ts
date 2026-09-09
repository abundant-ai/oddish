"use client";

import { useEffect, useRef } from "react";
import { SpanStatusCode, trace, type Span } from "@opentelemetry/api";

/**
 * User-perceived "time until I can use this" spans.
 *
 * The backend already reports per-route latency, but one screen is several
 * requests deep and a route p95 cannot answer "how long until the task page is
 * readable". These spans measure the thing people actually wait for: from the
 * moment a view is asked for until its content has been painted.
 *
 * Emitted under service ``oddish-frontend`` (see ``lib/observability.ts``), so
 * they are only recorded when ``NEXT_PUBLIC_LOGFIRE_ENABLED`` is on. Without a
 * registered tracer provider the OpenTelemetry API hands back a no-op tracer,
 * so every call here stays cheap and side-effect free in that case.
 *
 * These spans do NOT parent the API spans underneath them. The span opens in an
 * effect, which React runs after render, by which time SWR has already started
 * fetching; and ``startSpan`` does not install an active context the way
 * ``startActiveSpan`` would. Making a stopwatch that outlives its own callback
 * into the ambient parent of fetches begun elsewhere is not something the
 * browser context manager can do. Correlate instead on the recorded
 * ``oddish.task_id`` / ``oddish.trial_id`` and the span's time bounds.
 */

const TRACER_NAME = "oddish-frontend";

/**
 * Above this, treat page-load attribution as implausible and fall back to the
 * interaction clock. A component that first mounts a minute after navigation
 * started (a lazily opened drawer, a background tab) is not describing how long
 * a person waited for it.
 */
export const MAX_PAGE_LOAD_ATTRIBUTION_MS = 60_000;

export type OpenLatencyStartSource = "page-load" | "interaction";

export type ResolveStartTimeInput = {
  /** Epoch milliseconds when the view was asked for (component mount). */
  now: number;
  /** ``performance.timeOrigin`` — epoch ms at which navigation began. */
  timeOrigin: number;
  /** ``PerformanceNavigationTiming.type``, or null when unavailable. */
  navigationType: string | null;
  /** Whether this is the first measured open since the document loaded. */
  firstOpenOnPage: boolean;
  /** Path the document itself was served for, captured at module load. */
  initialPath: string | null;
  /** Path this open is happening on. */
  currentPath: string | null;
  maxAttributableMs?: number;
};

export type ResolvedStartTime = {
  /** Epoch milliseconds to open the span at. */
  startTime: number;
  source: OpenLatencyStartSource;
};

/**
 * Decide which clock an open should be measured from.
 *
 * A hard navigation (address bar, refresh, deep link) spends real time in DNS,
 * TLS, the document request, and hydration before React mounts anything. Timing
 * from mount would silently discard all of it and flatter exactly the slow path
 * we care about, so the landing open is backdated to ``performance.timeOrigin``.
 *
 * A client-side route change has no such prologue: mount is the interaction, so
 * those opens use the current clock.
 *
 * Telling the two apart needs the path, not just the navigation type.
 * ``PerformanceNavigationTiming.type`` describes the DOCUMENT and stays
 * ``navigate`` for the entire single-page session, so it cannot distinguish
 * "landed here" from "clicked here twenty seconds later". Comparing against the
 * path the document was served for can: only an open on that same route can
 * legitimately claim the document's load time. Someone who lands on the task
 * list, browses, then opens a task is measured from the click.
 *
 * Pure and exported for tests — the React wrapper below owns only the effect
 * bookkeeping.
 */
export function resolveStartTime({
  now,
  timeOrigin,
  navigationType,
  firstOpenOnPage,
  initialPath,
  currentPath,
  maxAttributableMs = MAX_PAGE_LOAD_ATTRIBUTION_MS,
}: ResolveStartTimeInput): ResolvedStartTime {
  const hardNavigation =
    navigationType === "navigate" ||
    navigationType === "reload" ||
    navigationType === "back_forward";

  if (!firstOpenOnPage || !hardNavigation) {
    return { startTime: now, source: "interaction" };
  }
  if (initialPath === null || currentPath === null) {
    return { startTime: now, source: "interaction" };
  }
  if (initialPath !== currentPath) {
    return { startTime: now, source: "interaction" };
  }
  if (!Number.isFinite(timeOrigin) || timeOrigin <= 0 || timeOrigin > now) {
    return { startTime: now, source: "interaction" };
  }
  if (now - timeOrigin > maxAttributableMs) {
    return { startTime: now, source: "interaction" };
  }
  return { startTime: timeOrigin, source: "page-load" };
}

export type OpenLatencyAttributes = Record<string, string | number | boolean>;

export type UseOpenLatencySpanOptions = {
  /** Span name, e.g. ``ui.task.open``. Keep it a bounded, low-cardinality set. */
  name: string;
  /**
   * Identity of what is being opened (task id, trial id, file path). A change
   * abandons the in-flight span and starts a fresh one, so switching files mid
   * load is recorded as two opens rather than one impossibly long one. ``null``
   * measures nothing.
   */
  subject: string | null;
  /** True once the content a person came for is on screen. */
  ready: boolean;
  /** True when the open failed and the content will never arrive. */
  failed?: boolean;
  /**
   * Recorded on the span. Spans are individual records, so high-cardinality
   * values (ids, paths) are fine here — unlike the metric dimension rules in
   * ``docs/observability/README.md``.
   */
  attributes?: OpenLatencyAttributes;
};

type PendingOpen = {
  span: Span;
  subject: string;
  settled: boolean;
};

/** One module-level latch: only the first open on a page can claim load time. */
let pageLoadClaimed = false;

/**
 * The route the document was served for. Read once at module evaluation, which
 * happens during the initial load and never again for the life of the tab, so
 * it stays the landing path across every client-side navigation afterwards.
 */
const INITIAL_PATH =
  typeof window === "undefined" ? null : window.location.pathname;

/** Test seam — resets the page-load latch. */
export function resetPageLoadAttribution(): void {
  pageLoadClaimed = false;
}

function currentPath(): string | null {
  return typeof window === "undefined" ? null : window.location.pathname;
}

function navigationType(): string | null {
  if (typeof performance === "undefined") return null;
  try {
    const [entry] = performance.getEntriesByType(
      "navigation"
    ) as PerformanceNavigationTiming[];
    return entry?.type ?? null;
  } catch {
    return null;
  }
}

/**
 * Run after the browser has painted. A single rAF callback fires *before* the
 * paint it belongs to, so the second frame is the first moment the content is
 * genuinely on screen.
 */
function afterPaint(fn: () => void): () => void {
  if (typeof requestAnimationFrame !== "function") {
    fn();
    return () => {};
  }
  let inner = 0;
  const outer = requestAnimationFrame(() => {
    inner = requestAnimationFrame(fn);
  });
  return () => {
    cancelAnimationFrame(outer);
    if (inner) cancelAnimationFrame(inner);
  };
}

export function useOpenLatencySpan({
  name,
  subject,
  ready,
  failed = false,
  attributes,
}: UseOpenLatencySpanOptions): void {
  const pending = useRef<PendingOpen | null>(null);
  // Read inside effects without making them re-run on every render.
  const latestAttributes = useRef<OpenLatencyAttributes | undefined>(
    attributes
  );
  latestAttributes.current = attributes;

  const settle = useRef(
    (outcome: "ready" | "error" | "abandoned", error?: boolean) => {
      const open = pending.current;
      if (!open || open.settled) return;
      open.settled = true;
      open.span.setAttribute("outcome", outcome);
      for (const [key, value] of Object.entries(
        latestAttributes.current ?? {}
      )) {
        open.span.setAttribute(key, value);
      }
      open.span.setStatus({
        code: error ? SpanStatusCode.ERROR : SpanStatusCode.OK,
      });
      open.span.end();
    }
  ).current;

  // Start/restart. Keyed on the subject so a switch closes the old open.
  useEffect(() => {
    if (subject === null) {
      settle("abandoned");
      pending.current = null;
      return;
    }
    if (pending.current?.subject === subject && !pending.current.settled) {
      return;
    }
    settle("abandoned");

    const firstOpenOnPage = !pageLoadClaimed;
    pageLoadClaimed = true;
    const { startTime, source } = resolveStartTime({
      now: Date.now(),
      timeOrigin:
        typeof performance === "undefined" ? 0 : performance.timeOrigin,
      navigationType: navigationType(),
      firstOpenOnPage,
      initialPath: INITIAL_PATH,
      currentPath: currentPath(),
    });

    pending.current = {
      subject,
      settled: false,
      span: trace.getTracer(TRACER_NAME).startSpan(name, {
        startTime,
        attributes: { "open.subject": subject, "open.start_source": source },
      }),
    };
  }, [name, subject, settle]);

  // Finish once the content is painted, or immediately on failure.
  //
  // ``subject`` is a dependency even though it is unused in the body: a switch
  // to something already loaded (a cached file, a task whose data SWR is still
  // holding under `keepPreviousData`) leaves `ready` true throughout, so
  // without it this effect would not re-run and the fresh span would sit open
  // until unmount and record `abandoned` — losing precisely the fast opens.
  useEffect(() => {
    if (!pending.current || pending.current.settled) return;
    if (failed) {
      settle("error", true);
      return;
    }
    if (!ready) return;
    return afterPaint(() => settle("ready"));
  }, [ready, failed, subject, settle]);

  // A person who navigates away before the content lands is the signal we most
  // need; dropping those opens would quietly improve every percentile.
  useEffect(() => () => settle("abandoned"), [settle]);
}
