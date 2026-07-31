"use client";

import { useEffect, useRef, useState } from "react";

const POLL_INTERVAL_MS = 5_000;
// The same-origin health route holds its backend probe open for up to 110s
// (outlasting a Modal cold start), so the client must not abort before the
// route can answer.
const PROBE_TIMEOUT_MS = 125_000;
// A warm backend answers the health route well under a second. A first
// probe that only succeeds after this long means the backend was cold or
// down while the page server-rendered, so its data likely failed -- treat
// it as a recovery even though no failed probe preceded it.
const SLOW_FIRST_PROBE_MS = 5_000;
const RELOAD_GUARD_KEY = "oddish-preview-health-reloaded-at";
// A slow-but-healthy backend must not reload the page on every visit, so
// recovery reloads are rate-limited per tab.
const RELOAD_GUARD_WINDOW_MS = 60_000;

function reloadOnce() {
  try {
    const last = Number(sessionStorage.getItem(RELOAD_GUARD_KEY) || 0);
    if (Date.now() - last < RELOAD_GUARD_WINDOW_MS) {
      return;
    }
    sessionStorage.setItem(RELOAD_GUARD_KEY, String(Date.now()));
  } catch {
    // sessionStorage unavailable -- reload without the guard.
  }
  window.location.reload();
}

/**
 * Polls the same-origin /api/preview-backend-health route and, while the
 * preview backend is unreachable, shows a prominent full-width banner
 * under the preview strip. The Vercel preview deploys in parallel with
 * the Modal backend, so the frontend can go live minutes before its
 * backend exists; this is the user-facing cover for that window. The
 * banner deliberately does not suppress the page's normal API error
 * states (production needs those untouched) -- it explains them instead.
 * Once the backend turns ready after having been down, the page reloads
 * so server-rendered data that failed during the outage is refetched.
 *
 * The probe deliberately goes through a Next.js route handler instead of
 * fetching the backend directly: the hosted backend has no unauthenticated
 * health route and its CORS allowlist does not include preview origins, so
 * a direct browser fetch can never observe a success.
 */
export function PreviewBackendStatus({ enabled }: { enabled: boolean }) {
  const [ready, setReady] = useState<boolean | null>(null);
  const wasDown = useRef(false);

  useEffect(() => {
    if (!enabled) {
      return;
    }
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let firstProbe = true;

    const probe = async () => {
      let healthy = false;
      const startedAt = Date.now();
      try {
        const res = await fetch("/api/preview-backend-health", {
          cache: "no-store",
          signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
        });
        if (res.ok) {
          const body = await res.json().catch(() => null);
          healthy = body?.ready === true;
        }
      } catch {
        // Probe route unreachable -- treat as not ready and keep polling.
      }
      if (cancelled) {
        return;
      }
      const slowFirstProbe =
        firstProbe && Date.now() - startedAt > SLOW_FIRST_PROBE_MS;
      firstProbe = false;
      if (healthy) {
        setReady(true);
        if (wasDown.current || slowFirstProbe) {
          reloadOnce();
        }
        return;
      }
      wasDown.current = true;
      setReady(false);
      timer = setTimeout(probe, POLL_INTERVAL_MS);
    };

    probe();
    return () => {
      cancelled = true;
      if (timer) {
        clearTimeout(timer);
      }
    };
  }, [enabled]);

  if (!enabled || ready !== false) {
    return null;
  }

  return (
    <div
      role="status"
      className="border-b border-amber-500/50 bg-amber-200 text-amber-950 dark:border-amber-300/30 dark:bg-amber-500/25 dark:text-amber-50"
    >
      <div className="mx-auto flex max-w-(--breakpoint-2xl) items-center gap-3 px-4 py-3 text-sm">
        <span
          aria-hidden="true"
          className="h-4 w-4 shrink-0 animate-spin rounded-full border-2 border-current border-t-transparent"
        />
        <span>
          <span className="font-semibold">
            The preview backend is still deploying.
          </span>{" "}
          Data will fail to load until it&apos;s ready — any API errors below
          are expected. This page will reload automatically.
        </span>
      </div>
    </div>
  );
}
