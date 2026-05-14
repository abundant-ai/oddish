"use client";

import { SWRConfig } from "swr";

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <SWRConfig
      value={{
        dedupingInterval: 5000,
        revalidateOnFocus: false,
        revalidateOnReconnect: false,
        errorRetryCount: 2,
      }}
    >
      {children}
    </SWRConfig>
  );
}
