"use client";

import { useEffect, useState } from "react";
import { previewBackendStatus } from "@/lib/backend-config";

/**
 * Top-of-page red banner shown whenever this build is a Vercel preview
 * but ``NEXT_PUBLIC_API_URL`` is NOT a per-PR Modal preview URL — i.e.
 * the workflow's "Point Vercel preview to Modal preview" step never
 * ran (or ran against the wrong env). Without this banner, the only
 * signal of a misconfigured preview is "API calls to prod silently
 * succeed" — exactly the dangerous default we want to refuse.
 */
export function PreviewMisconfigBanner() {
  const [status, setStatus] = useState<ReturnType<
    typeof previewBackendStatus
  > | null>(null);

  useEffect(() => {
    setStatus(previewBackendStatus());
  }, []);

  if (!status?.isMisconfigured) return null;

  return (
    <div className="sticky top-0 z-[60] border-b-2 border-red-500 bg-red-600 px-4 py-2 text-sm text-white">
      <div className="mx-auto flex max-w-(--breakpoint-2xl) items-center gap-3">
        <span className="font-bold uppercase tracking-wide">⚠ Misconfigured preview</span>
        <span>
          This Vercel preview is built against{" "}
          <code className="rounded bg-red-800/40 px-1 py-0.5">
            {status.apiUrl}
          </code>{" "}
          — that&apos;s not a per-PR Modal preview URL. API calls are blocked.
          Re-run the deploy workflow once the per-PR Modal app exists.
        </span>
      </div>
    </div>
  );
}
