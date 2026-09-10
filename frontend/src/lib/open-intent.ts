"use client";

/**
 * When a person asked for a view, recorded where they asked — the click.
 *
 * ``useOpenLatencySpan`` starts its clock when the destination component
 * mounts, and for an in-app navigation that is far too late. Clicking a task
 * has to download the route's JavaScript chunk, run it, and render the
 * component before any effect in it fires; a click that spends two seconds
 * getting the task page on screen and another hundred milliseconds fetching
 * its results would be recorded as roughly a hundred milliseconds. The
 * measurement would be at its most flattering exactly when the app was at its
 * worst, and no amount of care inside the hook can recover an interval that
 * ended before the hook existed.
 *
 * So the click records the timestamp and the destination collects it. A very
 * small module-scope map is the only thing that survives the route change —
 * React state does not, because the component holding it is being replaced.
 */

export type OpenIntent = {
  /** Span name the intent is for, e.g. ``ui.task.open``. */
  name: string;
  /** Subject the intent is for: task id, file path, trial id. */
  subject: string;
  /** Epoch ms at which the person clicked. */
  at: number;
};

/**
 * An intent older than this is treated as unrelated. A click that has not
 * produced its view within ten seconds has almost certainly been abandoned or
 * superseded, and pairing it with a later mount would invent a wait nobody had.
 */
export const MAX_INTENT_AGE_MS = 10_000;

const intents = new Map<string, OpenIntent>();

/**
 * Compose the map key. NUL cannot occur in a span name or in any subject we
 * use -- task id, trial id, file path -- so no pair of values can collide on
 * it, which a space separator could not promise for paths.
 */
function key(name: string, subject: string): string {
  return `${name}\u0000${subject}`;
}

/**
 * Record that someone just asked for a view. Safe to call from any click
 * handler; an intent nobody collects is simply overwritten or aged out.
 */
export function markOpenIntent(
  name: string,
  subject: string,
  at: number = Date.now()
): void {
  intents.set(key(name, subject), { name, subject, at });
}

/**
 * Collect and remove the intent for this view, if one is waiting.
 *
 * Consumed rather than read so a second mount of the same subject — a remount,
 * a revalidation — cannot reuse a click that has already been accounted for.
 */
export function takeOpenIntent(
  name: string,
  subject: string
): OpenIntent | null {
  const found = intents.get(key(name, subject));
  if (!found) return null;
  intents.delete(key(name, subject));
  return found;
}

/** Test seam. */
export function clearOpenIntents(): void {
  intents.clear();
}

export type ResolveInteractionStartInput = {
  /** Epoch ms the destination component mounted. */
  mountedAt: number;
  /** Epoch ms of the click that asked for it, when one was recorded. */
  intentAt: number | null;
  maxIntentAgeMs?: number;
};

/**
 * Choose the start of an interaction open: the click when we have a usable
 * one, otherwise the mount.
 *
 * Pure and exported for tests — the mount timestamp alone cannot express the
 * bug this exists to fix, so the test has to be able to supply both.
 */
export function resolveInteractionStart({
  mountedAt,
  intentAt,
  maxIntentAgeMs = MAX_INTENT_AGE_MS,
}: ResolveInteractionStartInput): {
  startTime: number;
  source: "click" | "mount";
} {
  if (intentAt === null || !Number.isFinite(intentAt)) {
    return { startTime: mountedAt, source: "mount" };
  }
  // A click cannot happen after the mount it caused. A clock adjustment or a
  // stale entry is the likelier explanation, and neither should be measured.
  if (intentAt > mountedAt) {
    return { startTime: mountedAt, source: "mount" };
  }
  if (mountedAt - intentAt > maxIntentAgeMs) {
    return { startTime: mountedAt, source: "mount" };
  }
  return { startTime: intentAt, source: "click" };
}

/**
 * Record a click on any in-app link to a task page.
 *
 * Stamping call sites one at a time does not hold. Tasks are linked from the
 * task cards, the experiment table, trial panels, the delivery board and the
 * admin user pages, and a link added next month would silently fall back to
 * the mount clock and quietly under-report. One capture-phase listener on the
 * document covers every ``<a>`` Next.js renders, including ones nobody has
 * written yet.
 *
 * Capture phase so the stamp lands before any handler that calls
 * ``preventDefault`` or navigates; the listener only reads.
 */
let navigationCaptureInstalled = false;

export function installNavigationIntentCapture(): void {
  if (navigationCaptureInstalled || typeof document === "undefined") return;
  navigationCaptureInstalled = true;

  document.addEventListener(
    "click",
    (event) => {
      try {
        const target = event.target;
        if (!(target instanceof Element)) return;
        const anchor = target.closest("a[href]");
        if (!anchor) return;
        const href = anchor.getAttribute("href");
        if (!href || href.startsWith("http")) return;

        const taskId = taskIdFromPath(new URL(href, location.origin).pathname);
        if (taskId) markOpenIntent("ui.task.open", taskId);
      } catch {
        /* a stamp is never worth breaking a navigation over */
      }
    },
    { capture: true }
  );
}

/**
 * The task id in a dashboard path, or null when the path is something else.
 *
 * Authenticated URLs carry an organization slug (`/orgs/{slug}/tasks/{id}`),
 * and the unprefixed form still exists, so both are accepted. Deeper paths
 * such as `/tasks/{id}/probe` are a different page and are not this open.
 *
 * Pure and exported for tests.
 */
export function taskIdFromPath(pathname: string): string | null {
  const segments = pathname.split("/").filter(Boolean);
  const rest = segments[0] === "orgs" ? segments.slice(2) : segments;
  if (rest.length !== 2 || rest[0] !== "tasks") return null;
  try {
    return decodeURIComponent(rest[1]);
  } catch {
    return rest[1];
  }
}
