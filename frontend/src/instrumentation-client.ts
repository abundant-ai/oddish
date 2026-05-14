/**
 * Next.js client-side instrumentation entrypoint.
 *
 * Loaded by Next.js at the top of the browser bundle, BEFORE React
 * hydration, the first router prefetch, or any `<Link>` mouse-over
 * triggers a `fetch`. That ordering matters: until
 * `@opentelemetry/instrumentation-fetch` has patched `globalThis.fetch`,
 * outbound requests ship without a `traceparent` header, so the
 * Vercel-side `oddish-frontend-edge` spans for the matching request
 * arrive at Logfire as roots with no browser parent — the exact
 * "orphan span" shape we are trying to kill.
 *
 * Previously this lived in a `useEffect` inside `<Providers>`, which
 * ran AFTER hydration and lost the race against link prefetches on
 * the initial paint.
 */

import { ensureLogfireConfigured } from "@/lib/observability";

ensureLogfireConfigured();
