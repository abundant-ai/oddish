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

// Emit the active edge span as a `traceparent` value inside Server-Timing
// so the browser's `instrumentation-document-load` can attach its
// navigation span as a child — browsers don't propagate trace context for
// top-level navigations, so without this server spans land in Logfire
// with no browser parent.
function attachTraceparent(response: NextResponse): NextResponse {
  const span = trace.getActiveSpan();
  if (!span) return response;
  const ctx = span.spanContext();
  // All-zero ids = non-recording span; useless as a parent.
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
