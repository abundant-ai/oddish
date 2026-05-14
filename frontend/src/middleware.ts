import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";
import { trace } from "@opentelemetry/api";
import { NextResponse } from "next/server";

// Define public routes that don't require authentication
// Note: `/experiments(.*)` is intentionally public so that link-unfurl bots
// (Slack, Twitter, etc.) can fetch the page shell and read the OpenGraph /
// Twitter metadata. Real unauthenticated users are redirected to sign-in by
// the `(app)` layout via `<RedirectToSignIn />`, and the page only fetches
// data when the user is authenticated (see `getInitialTasks`).
const isPublicRoute = createRouteMatcher([
  "/",
  "/sign-in(.*)",
  "/sign-up(.*)",
  "/share(.*)",
  "/datasets(.*)",
  "/experiments(.*)",
  "/api/public(.*)",
]);

// Emit the active span as a `traceparent` value inside a Server-Timing
// header so the browser's `@opentelemetry/instrumentation-document-load`
// has something to attach its navigation span to. Without this, a fresh
// document load arrives at Next.js with no `traceparent` header (browsers
// don't propagate trace context for top-level navigations), the edge
// runtime creates `middleware` + `GET /(...)/page` spans as roots, and
// Logfire shows the trace as orphaned because no browser parent ever
// joins it.
function attachTraceparent(response: NextResponse): NextResponse {
  const span = trace.getActiveSpan();
  if (!span) return response;
  const ctx = span.spanContext();
  // `00000…` ids mean the SDK handed us a non-recording span (sampler
  // dropped, exporter disabled, etc.) — useless as a parent.
  if (!ctx.traceId || !ctx.spanId || /^0+$/.test(ctx.traceId)) {
    return response;
  }
  const flags = (ctx.traceFlags & 0xff).toString(16).padStart(2, "0");
  const traceparent = `00-${ctx.traceId}-${ctx.spanId}-${flags}`;
  const entry = `traceparent;desc="${traceparent}"`;
  const existing = response.headers.get("Server-Timing");
  response.headers.set(
    "Server-Timing",
    existing ? `${existing}, ${entry}` : entry
  );
  return response;
}

export default clerkMiddleware(async (auth, request) => {
  // Protect all routes except public ones
  if (!isPublicRoute(request)) {
    await auth.protect();
  }
  return attachTraceparent(NextResponse.next());
});

export const config = {
  matcher: [
    // Skip Next.js internals and all static files
    "/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)",
    // Always run for API routes
    "/(api|trpc)(.*)",
  ],
};
