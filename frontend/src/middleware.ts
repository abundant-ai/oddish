import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";

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

export default clerkMiddleware(async (auth, request) => {
  // Protect all routes except public ones
  if (!isPublicRoute(request)) {
    await auth.protect();
  }
});

export const config = {
  matcher: [
    // Skip Next.js internals and all static files
    "/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)",
    // Always run for API routes
    "/(api|trpc)(.*)",
  ],
};
