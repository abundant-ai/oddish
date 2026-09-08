import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";
import { trace } from "@opentelemetry/api";
import { NextResponse } from "next/server";
import {
  isSluggedExperimentPath,
  ORG_SYNC_PATTERNS,
  resolveOrgRequest,
} from "@/lib/org-path";

const isPublicRoute = createRouteMatcher([
  "/",
  "/sign-in(.*)",
  "/sign-up(.*)",
  "/share(.*)",
  "/datasets(.*)",
  // Public so link-unfurl bots (Slack, Twitter) can read OG/Twitter meta;
  // real unauthed users are redirected by the (app) layout and no data is
  // fetched until authed. Signed-in users live at
  // /orgs/{orgSlug}/experiments/…; that slugged path is also public below
  // so pasting the address-bar URL still unfurls. Do not gate either
  // without preserving unfurls.
  "/experiments(.*)",
  "/orgs/:slug/experiments(.*)",
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
  const entry = `traceparent;desc="00-${ctx.traceId}-${ctx.spanId}-${flags}"`;
  const existing = response.headers.get("Server-Timing");
  response.headers.set(
    "Server-Timing",
    existing ? `${existing}, ${entry}` : entry,
  );
  return response;
}

export default clerkMiddleware(
  async (auth, request) => {
    if (
      !isPublicRoute(request) &&
      !isSluggedExperimentPath(request.nextUrl.pathname)
    ) {
      await auth.protect();
    }

    const { userId, orgSlug } = await auth();
    const decision = resolveOrgRequest({
      pathname: request.nextUrl.pathname,
      userId,
      orgSlug,
    });

    if (decision.action === "redirect") {
      const url = request.nextUrl.clone();
      url.pathname = decision.pathname;
      return attachTraceparent(NextResponse.redirect(url, decision.status));
    }

    if (decision.action === "rewrite") {
      const url = request.nextUrl.clone();
      url.pathname = decision.pathname;
      return attachTraceparent(NextResponse.rewrite(url));
    }

    return attachTraceparent(NextResponse.next());
  },
  {
    organizationSyncOptions: {
      organizationPatterns: ORG_SYNC_PATTERNS,
    },
  },
);

export const config = {
  matcher: [
    "/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)",
    "/(api|trpc)(.*)",
  ],
};
