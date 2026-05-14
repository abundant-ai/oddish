// Loaded by Next.js before React mounts, so `fetch` is OTEL-patched
// before the first router prefetch fires.

import { ensureLogfireConfigured } from "@/lib/observability";

ensureLogfireConfigured();
