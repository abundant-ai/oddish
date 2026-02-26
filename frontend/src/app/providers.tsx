"use client";

import { SWRConfig } from "swr";

export function Providers({ children }: { children: React.ReactNode }) {
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
