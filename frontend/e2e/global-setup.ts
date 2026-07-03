import { clerkSetup } from "@clerk/testing/playwright";

// Gate on the exact same env condition as the spec: if any piece is missing
// the spec skips, so calling into Clerk here would be wasted at best — and an
// unconditional clerkSetup() would throw on a bare checkout and fail the whole
// run before any test decides to skip.
export default async function globalSetup() {
  const hasClerkEnv =
    !!process.env.E2E_CLERK_EMAIL &&
    !!process.env.CLERK_SECRET_KEY &&
    !!process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY;
  if (!hasClerkEnv) return;

  await clerkSetup();
}
