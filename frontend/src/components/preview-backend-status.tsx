"use client";

import { useEffect, useRef, useState } from "react";

const POLL_INTERVAL_MS = 5_000;
// A probe against a cold-starting Modal backend hangs until the container
// boots (~30s measured); the timeout must outlast that or every attempt
// aborts mid-boot and the badge never clears.
const PROBE_TIMEOUT_MS = 120_000;

/**
 * Polls the preview backend's /health endpoint and, while it is unreachable
 * or its database is disconnected, shows a "still deploying" chip in the
 * preview banner. The Vercel preview deploys in parallel with the Modal
 * backend, so the frontend can go live minutes before its backend exists;
 * this is the user-facing cover for that window. Once the backend turns
 * healthy after having been down, the page reloads so server-rendered data
 * that failed during the outage is refetched.
 */
export function PreviewBackendStatus({
  backendUrl,
  enabled,
}: {
  backendUrl: string;
  enabled: boolean;
}) {
  const [ready, setReady] = useState<boolean | null>(null);
  const wasDown = useRef(false);

  useEffect(() => {
    if (!enabled || !backendUrl) {
      return;
    }
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const healthUrl = `${backendUrl.replace(/\/+$/, "")}/health`;

    const probe = async () => {
      let healthy = false;
      try {
        const res = await fetch(healthUrl, {
          cache: "no-store",
          signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
        });
        if (res.ok) {
          const body = await res.json().catch(() => null);
          healthy = !body || body.status === "healthy";
        }
      } catch {
        // Unreachable or timed out -- the backend isn't up yet.
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
  }, [backendUrl, enabled]);

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
