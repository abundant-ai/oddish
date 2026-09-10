"use client";

import { useEffect, useRef } from "react";
import { SpanStatusCode, trace } from "@opentelemetry/api";
import { flushTelemetry } from "@/lib/telemetry-flush";
import { resolveInteractionStart, takeOpenIntent } from "@/lib/open-intent";

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
  /** Path the document itself was fetched from (navigation timing entry). */
  documentPath: string | null;
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
 * "landed here" from "clicked here twenty seconds later". The same entry's
 * address can: only an open on the route the document was actually fetched
 * from may claim that document's load time. Someone who lands on the task
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
  documentPath,
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
  if (documentPath === null || currentPath === null) {
    return { startTime: now, source: "interaction" };
  }
  if (documentPath !== currentPath) {
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
   * Whether this open is allowed to claim the document's load time when it is
   * the landing view. Opt-in, and false by default, because most opens cannot
   * possibly be a landing: a file preview or a trajectory is reached by
   * clicking, so its wait begins at the click no matter how the page was
   * reached.
   *
   * Without the opt-in the single ``pageLoadClaimed`` latch is shared across
   * every span name, so on a route with no page-level open (an experiment
   * page, a trial drawer opened first) the first drawer to appear would claim
   * it — backdating a deliberate click to ``performance.timeOrigin`` and
   * folding in however long the person spent reading before clicking. Set it
   * only on the open that represents the route itself.
   */
  claimsPageLoad?: boolean;
  /**
   * Recorded on the span. Spans are individual records, so high-cardinality
   * values (ids, paths) are fine here — unlike the metric dimension rules in
   * ``docs/observability/README.md``.
   */
  attributes?: OpenLatencyAttributes;
};

type PendingOpen = {
  name: string;
  subject: string;
  /** Epoch ms the wait began; the span is not created until it ends. */
  startTime: number;
  startSource: OpenLatencyStartSource;
  /** Which moment ``startTime`` came from: the document, a click, or mount. */
  clock: "page-load" | "click" | "mount";
  settled: boolean;
  /** Whether the caller is reporting a failure right now. */
  failing: boolean;
  /** Whether any failure was seen during this open, recovered or not. */
  sawError: boolean;
  /**
   * This open's own attributes, held here rather than read from a shared ref
   * at settle time. Switching from task A to B re-renders with B's attributes
   * before the effect settles A, so a shared ref would stamp B's identifiers
   * onto A's record -- a span reading ``open.subject=A`` next to
   * ``oddish.task_id=B``. Since those identifiers are how a slow open is
   * traced back to its backend requests, that mismatch points at the wrong
   * task. Only the matching subject may update this copy.
   */
  attributes: OpenLatencyAttributes | undefined;
};

/** One module-level latch: only the first open on a page can claim load time. */
let pageLoadClaimed = false;

/** Test seam — resets the page-load latch. */
export function resetPageLoadAttribution(): void {
  pageLoadClaimed = false;
}

/**
 * Whether the page is hidden right now.
 *
 * An open that BEGINS hidden -- the tab a cmd-click or middle-click put in the
 * background -- cannot be measured. Nobody is waiting on it, so its duration is
 * however long until someone happens to look; ``visibilitychange`` never fires
 * because nothing changed, the tab was born hidden; and ``requestAnimationFrame``
 * is frozen there, so ``afterPaint`` never runs to settle it. The open would
 * either surface minutes later with all that idle time recorded as latency, or
 * be dropped when the tab is closed unlooked-at. Both pollute the ``ready``
 * percentiles, and opening several tabs at once is a normal way to work.
 */
function documentHidden(): boolean {
  if (typeof document === "undefined") return false;
  return document.visibilityState === "hidden";
}

function currentPath(): string | null {
  return typeof window === "undefined" ? null : window.location.pathname;
}

/**
 * The navigation timing entry, which describes the DOCUMENT: its type and the
 * address it was fetched from. Both are needed together and come from one read.
 *
 * Deliberately not a module-scope constant. This module is code-split with the
 * task routes, so it first evaluates when one of those routes loads — which,
 * after a click from the task list, is long after the document did. Capturing
 * the path at module evaluation would therefore record the task route as the
 * landing route and hand it the document's load time, reintroducing the very
 * inflation the path check exists to stop. The browser's own navigation entry
 * has no such ambiguity: it is created once per document and soft navigations
 * never touch it.
 */
function documentNavigation(): { type: string | null; path: string | null } {
  if (typeof performance === "undefined") return { type: null, path: null };
  try {
    const [entry] = performance.getEntriesByType(
      "navigation"
    ) as PerformanceNavigationTiming[];
    if (!entry) return { type: null, path: null };
    let path: string | null = null;
    try {
      path = new URL(entry.name).pathname;
    } catch {
      path = null;
    }
    return { type: entry.type ?? null, path };
  } catch {
    return { type: null, path: null };
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

const openSpans = new Set<PendingOpen>();

type AbandonReason = "unmount" | "page-hidden";

/**
 * End an open, creating its span now and backdating the start.
 *
 * The span is deliberately not created when the wait begins. ``Logfire`` is
 * configured from a non-blocking dynamic ``import()`` in
 * ``instrumentation-client.ts``, so on a hard navigation a component can mount
 * and ask for a tracer before that heavy SDK has finished loading — and the
 * OpenTelemetry API answers with a no-op tracer whose spans go nowhere. That
 * dropped precisely the landing open, the cold measurement worth the most, and
 * consumed the one-shot page-load latch on the way out so no later open could
 * claim it either.
 *
 * Creating the span at the END instead removes the race: by then the SDK has
 * had the whole load to arrive, and an explicit ``startTime`` makes the
 * recorded duration identical to what a span opened at mount would have shown.
 * The only thing given up is watching an open in flight, which nothing needs.
 */
function settleOpen(
  open: PendingOpen,
  outcome: "ready" | "error" | "abandoned",
  options: { reason?: AbandonReason } = {}
): void {
  if (open.settled) return;
  open.settled = true;
  openSpans.delete(open);

  const span = trace.getTracer(TRACER_NAME).startSpan(open.name, {
    startTime: open.startTime,
    attributes: {
      "open.subject": open.subject,
      "open.start_source": open.startSource,
      "open.clock": open.clock,
      outcome,
      "open.saw_error": open.sawError,
    },
  });
  if (options.reason) span.setAttribute("open.abandon_reason", options.reason);
  for (const [key, value] of Object.entries(open.attributes ?? {})) {
    span.setAttribute(key, value);
  }
  span.setStatus({
    code: outcome === "error" ? SpanStatusCode.ERROR : SpanStatusCode.OK,
  });
  span.end();
}

/**
 * How an open that never reached ``ready`` should be recorded. A failure that
 * is still current when the view goes away is a failure; anything else is
 * someone who stopped waiting.
 */
function unfinishedOutcome(open: PendingOpen): "error" | "abandoned" {
  return open.failing ? "error" : "abandoned";
}

let unloadHandlerInstalled = false;

/**
 * Close every still-open span when the page goes away.
 *
 * React's unmount cleanup does not run when a tab is closed, refreshed, or sent
 * to a typed-in URL, so on its own it misses the abandonments that matter most:
 * a person giving up on a slow load closes the tab, they do not politely
 * navigate within the app first. Worse, ``lib/observability.ts`` flushes the
 * exporter on ``pagehide``, so those spans were still unended at flush time and
 * shipped nowhere at all — silently deleting the slowest opens and biasing
 * every percentile downwards.
 *
 * Hooked to ``visibilitychange`` rather than ``pagehide`` for ordering.
 * ``lib/observability.ts`` registers its flush handlers when Logfire configures
 * at app start, which is before this code-split module is ever imported, so a
 * ``pagehide`` listener added here would run after that flush and end its spans
 * too late to be exported. ``visibilitychange`` fires first and is followed by
 * the ``pagehide`` flush, which then finds the spans already ended.
 *
 * A tab switch during a load therefore also settles as abandoned. That is the
 * honest reading — nobody is waiting on a hidden tab — and
 * ``open.abandon_reason`` keeps it separable from a real unmount.
 */
function ensureUnloadHandler(): void {
  if (unloadHandlerInstalled || typeof document === "undefined") return;
  unloadHandlerInstalled = true;
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "hidden") return;
    for (const open of [...openSpans]) {
      settleOpen(open, unfinishedOutcome(open), { reason: "page-hidden" });
    }
    // Flush here rather than relying on the one in ``lib/observability.ts``.
    // ``flushTelemetry`` reaches the delegate behind the ``ProxyTracerProvider``;
    // calling ``forceFlush`` on the proxy itself finds no such method and skips
    // without complaint, which is what the previous attempt here did.
    // That handler is registered when Logfire configures at app start, before
    // this code-split module exists, and listeners run in registration order --
    // so it drains the exporter a moment before the spans above are created. A
    // tab CLOSE is still covered, because ``pagehide`` follows with another
    // flush. A tab SWITCH is not: no ``pagehide`` arrives, and the spans sit in
    // the batch queue of a page the browser is free to freeze or discard.
    // Those are the giving-up opens this handler exists to keep, so flush them
    // now instead of hoping the 1s batch timer runs in a backgrounded tab.
    flushTelemetry();
  });
}

export function useOpenLatencySpan({
  name,
  subject,
  ready,
  failed = false,
  claimsPageLoad = false,
  attributes,
}: UseOpenLatencySpanOptions): void {
  const pending = useRef<PendingOpen | null>(null);

  // This render's attributes, readable from the start effect without making it
  // a dependency. Call sites build the object inline, so it is a new reference
  // every render; depending on it would restart the span on each one.
  const latestAttributes = useRef<OpenLatencyAttributes | undefined>(attributes);
  latestAttributes.current = attributes;

  // Keep the in-flight open's attributes current, but only while they still
  // describe it. On a switch this render already holds the NEW subject's
  // attributes while the old open is still open, and copying those across
  // would relabel the old record with the new subject's identifiers.
  if (pending.current && !pending.current.settled) {
    if (pending.current.subject === subject) {
      pending.current.attributes = attributes;
    }
  }

  const settle = useRef(
    (
      outcome: "ready" | "error" | "abandoned",
      options: { reason?: AbandonReason } = {}
    ) => {
      const open = pending.current;
      if (open) settleOpen(open, outcome, options);
    }
  ).current;

  // Start/restart. Keyed on the subject so a switch closes the old open.
  useEffect(() => {
    if (subject === null) {
      if (pending.current) {
        settle(unfinishedOutcome(pending.current), { reason: "unmount" });
      }
      pending.current = null;
      return;
    }
    if (pending.current?.subject === subject && !pending.current.settled) {
      return;
    }
    if (pending.current) {
      settle(unfinishedOutcome(pending.current), { reason: "unmount" });
    }
    ensureUnloadHandler();

    // Claim the page-load latch before deciding whether to measure. The latch
    // means the document's load time has been ACCOUNTED FOR, which includes
    // forfeiting it: a landing that arrives hidden can never legitimately be
    // backdated, because by the time anyone looks, `performance.timeOrigin` is
    // however long the tab sat in the background. Leaving the latch free would
    // let a later remount on that same path claim it and file that idle time
    // as page-load latency.
    const firstOpenOnPage = claimsPageLoad && !pageLoadClaimed;
    if (claimsPageLoad) pageLoadClaimed = true;

    // Mounted while the tab is hidden. Two different situations arrive here
    // and they are told apart by whether a click was recorded.
    //
    // No click: a cmd-clicked or middle-clicked tab, which nobody is waiting
    // on. It also cannot be measured -- `requestAnimationFrame` is frozen so
    // it can never reach `ready`, and `visibilitychange` will not fire because
    // the tab was born hidden rather than changing. Skip it.
    //
    // A click: someone asked for this in this tab and switched away while it
    // loaded. That is a real wait, and an abandoned one. It can never reach
    // `ready` for the same frozen-frame reason, and the `visibilitychange`
    // that would have caught it has already passed, so record it now from the
    // click rather than losing it.
    if (documentHidden()) {
      const abandonedIntent = takeOpenIntent(name, subject);
      if (abandonedIntent) {
        settleOpen(
          {
            name,
            subject,
            startTime: abandonedIntent.at,
            startSource: "interaction",
            clock: "click",
            settled: false,
            failing: false,
            sawError: false,
            attributes: latestAttributes.current,
          },
          "abandoned",
          { reason: "page-hidden" }
        );
      }
      pending.current = null;
      return;
    }

    const navigation = documentNavigation();
    const { startTime, source } = resolveStartTime({
      now: Date.now(),
      timeOrigin:
        typeof performance === "undefined" ? 0 : performance.timeOrigin,
      navigationType: navigation.type,
      firstOpenOnPage,
      documentPath: navigation.path,
      currentPath: currentPath(),
    });

    // A page-load open already counts from the document request, which starts
    // before any click. An interaction open otherwise counts from mount, and
    // for a route change that is after the destination chunk has downloaded
    // and rendered -- so the click timestamp, when the navigation left one,
    // is the only way to include the part of the wait that happened before
    // this component existed.
    const intent = takeOpenIntent(name, subject);
    const interaction =
      source === "interaction"
        ? resolveInteractionStart({
            mountedAt: startTime,
            intentAt: intent?.at ?? null,
          })
        : null;

    const open: PendingOpen = {
      name,
      subject,
      startTime: interaction ? interaction.startTime : startTime,
      startSource: source,
      clock: interaction ? interaction.source : "page-load",
      settled: false,
      failing: false,
      sawError: false,
      // Snapshotted, not shared: from here on only a render whose subject
      // still matches may update it (see the guard above).
      attributes: latestAttributes.current,
    };
    pending.current = open;
    openSpans.add(open);
  }, [name, subject, claimsPageLoad, settle]);

  // Finish once the content is painted. A failure is recorded but is NOT
  // terminal: SWR retries (`errorRetryCount: 2` in `app/providers.tsx`) and the
  // task reader recovers a stale version 404, so ending the span on the first
  // error would settle `outcome=error` and leave the eventual successful paint
  // unmeasured -- and since `subject` has not changed, no new span would start.
  // The slow-but-recovered opens are exactly the ones worth seeing, so the
  // stopwatch keeps running and `open.saw_error` records that it was a bumpy
  // one. An error that is still current when the view goes away settles as
  // `error` through `unfinishedOutcome`.
  //
  // ``subject`` is a dependency even though it is unused in the body: a switch
  // to something already loaded (a cached file, a task whose data SWR is still
  // holding under `keepPreviousData`) leaves `ready` true throughout, so
  // without it this effect would not re-run and the fresh span would sit open
  // until unmount and record `abandoned` -- losing precisely the fast opens.
  useEffect(() => {
    const open = pending.current;
    if (!open || open.settled) return;
    open.failing = failed;
    if (failed) open.sawError = true;
    if (!ready) return;
    return afterPaint(() => settle("ready"));
  }, [ready, failed, subject, settle]);

  // A person who navigates away before the content lands is the signal we most
  // need; dropping those opens would quietly improve every percentile. Tab
  // closes and refreshes are caught by the visibility handler above instead,
  // because this cleanup does not run for them.
  useEffect(
    () => () => {
      const open = pending.current;
      if (open) settleOpen(open, unfinishedOutcome(open), { reason: "unmount" });
    },
    []
  );
}
