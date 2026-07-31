import { NextResponse } from "next/server";

// Server-side readiness probe for the preview banner's "backend still
// deploying" chip. The browser cannot probe the Modal backend directly:
// the hosted app (backend/api/app.py) has no /health route and its CORS
// allowlist does not include preview origins, so a cross-origin fetch
// always fails. This same-origin route probes /openapi.json instead --
// the same readiness signal the PR Preview workflow's gates use.
export const maxDuration = 120;
export const dynamic = "force-dynamic";

// Must outlast a Modal cold start (~30s measured, with headroom): a probe
// aborted mid-boot has repeatedly failed to converge on later polls too --
// the same failure mode wait_for_modal_ready.py documents (four PRs failed
// this way on 2026-07-20 with short per-request timeouts), which is why it
// uses a 120s per-request timeout.
const PROBE_TIMEOUT_MS = 110_000;

export async function GET() {
  if (process.env.NEXT_PUBLIC_ODDISH_PREVIEW !== "true") {
    return NextResponse.json(
      { error: "not a preview deployment" },
      { status: 404 },
    );
  }

  const apiUrl = process.env.NEXT_PUBLIC_API_URL;
  if (!apiUrl) {
    return NextResponse.json({ ready: false });
  }

  try {
    const res = await fetch(`${apiUrl.replace(/\/+$/, "")}/openapi.json`, {
      cache: "no-store",
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
    });
    return NextResponse.json({ ready: res.ok });
  } catch {
    return NextResponse.json({ ready: false });
  }
}
