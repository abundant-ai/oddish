"use client";

import { useEffect, useRef, useState } from "react";

const POLL_INTERVAL_MS = 5_000;
// The same-origin health route holds its backend probe open for up to 25s
// (covering a Modal cold start), so give it a little headroom.
const PROBE_TIMEOUT_MS = 30_000;

/**
 * Polls the same-origin /api/preview-backend-health route and, while the
 * preview backend is unreachable, shows a "still deploying" chip in the
 * preview banner. The Vercel preview deploys in parallel with the Modal
 * backend, so the frontend can go live minutes before its backend exists;
 * this is the user-facing cover for that window. Once the backend turns
 * ready after having been down, the page reloads so server-rendered data
 * that failed during the outage is refetched.
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

    const probe = async () => {
      let healthy = false;
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
      if (healthy) {
        setReady(true);
        if (wasDown.current) {
          window.location.reload();
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
    <>
      <span
        aria-hidden="true"
        className="text-amber-950/40 dark:text-amber-100/40"
      >
        ·
      </span>
      <span className="inline-flex items-center gap-1.5 font-medium">
        <span
          aria-hidden="true"
          className="h-1.5 w-1.5 animate-pulse rounded-full bg-current"
        />
        backend still deploying — reloads when ready
      </span>
    </>
  );
}
