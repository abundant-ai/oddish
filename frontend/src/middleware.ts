import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";
import { trace } from "@opentelemetry/api";
import { NextResponse } from "next/server";

const isPublicRoute = createRouteMatcher([
  "/",
  "/sign-in(.*)",
  "/sign-up(.*)",
  "/share(.*)",
  "/datasets(.*)",
  // Public so link-unfurl bots (Slack, Twitter) can read OG/Twitter meta;
  // real unauthed users are redirected by the (app) layout and no data is
  // fetched until authed. Do not gate this without preserving unfurls.
  "/experiments(.*)",
  "/api/public(.*)",
  "/api/client-traces(.*)",
]);

function attachTraceparent(response: NextResponse): NextResponse {
  const span = trace.getActiveSpan();
  if (!span) return response;
  const ctx = span.spanContext();
  if (!ctx.traceId || !ctx.spanId || /^0+$/.test(ctx.traceId)) {
    return response;
  }
  const flags = (ctx.traceFlags & 0xff).toString(16).padStart(2, "0");
  const traceparent = `00-${ctx.traceId}-${ctx.spanId}-${flags}`;
  const entry = `traceparent;desc="${traceparent}"`;
  const existing = response.headers.get("Server-Timing");
  response.headers.set(
    "Server-Timing",
    existing ? `${existing}, ${entry}` : entry,
  );
  return response;
}

export default clerkMiddleware(async (auth, request) => {
  if (!isPublicRoute(request)) {
    await auth.protect();
  }
  if (request.nextUrl.pathname === "/" && (await auth()).userId) {
    return NextResponse.redirect(new URL("/dashboard", request.url));
  }
  return attachTraceparent(NextResponse.next());
});

export const config = {
  matcher: [
    "/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)",
    "/(api|trpc)(.*)",
  ],
};
