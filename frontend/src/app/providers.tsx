"use client";

import { useEffect } from "react";
import { SWRConfig } from "swr";
import { installNavigationIntentCapture } from "@/lib/open-intent";

export function Providers({ children }: { children: React.ReactNode }) {
  // Mounted on every page, which is where this has to live: the click that
  // starts a task open happens on the page being left, not the one being
  // measured.
  useEffect(() => {
    installNavigationIntentCapture();
  }, []);

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
