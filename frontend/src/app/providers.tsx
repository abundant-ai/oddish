"use client";

import { useEffect } from "react";
import { SWRConfig } from "swr";
import { ensureLogfireConfigured } from "@/lib/observability";

export function Providers({ children }: { children: React.ReactNode }) {
  // Boot Logfire browser tracing exactly once per client load. We do
  // this in an effect (rather than at module scope) so it runs after
  // hydration and never on the server.
  useEffect(() => {
    ensureLogfireConfigured();
  }, []);

  return (
    <SWRConfig
      value={{
        dedupingInterval: 5000, // Don't refetch same key within 5s
        revalidateOnFocus: false, // Stop refetching on tab focus
        revalidateOnReconnect: false, // Don't refetch on reconnect
        errorRetryCount: 2, // Limit retries
      }}
    >
      {children}
    </SWRConfig>
  );
}
