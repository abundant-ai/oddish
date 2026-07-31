import { NextResponse } from "next/server";

// Server-side readiness probe for the preview banner's "backend still
// deploying" chip. The browser cannot probe the Modal backend directly:
// the hosted app (backend/api/app.py) has no /health route and its CORS
// allowlist does not include preview origins, so a cross-origin fetch
// always fails. This same-origin route probes /openapi.json instead --
// the same readiness signal the PR Preview workflow's gates use.
export const maxDuration = 30;
export const dynamic = "force-dynamic";

// Long enough that the first probe after a Modal scale-to-zero cold start
// (~30s measured) usually returns ready in one round trip; when it doesn't,
// the aborted request still triggered the container boot, so a following
// poll succeeds.
const PROBE_TIMEOUT_MS = 25_000;

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
