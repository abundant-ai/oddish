"use client";

import { useEffect, useMemo, useSyncExternalStore } from "react";
import { useAuth } from "@clerk/nextjs";
import { apiFetch } from "@/lib/api";
import { UserUiLayoutStore } from "@/lib/user-ui-layout";

export function useUserUiLayout(enabled: boolean) {
  const { isLoaded, userId, orgId } = useAuth();
  const identity =
    enabled && isLoaded && userId && orgId
      ? JSON.stringify([userId, orgId])
      : null;
  const store = useMemo(
    () =>
      new UserUiLayoutStore(
        identity ? (input, init) => apiFetch(String(input), init) : null
      ),
    [identity]
  );
  useEffect(() => store.start(), [store]);
  const snapshot = useSyncExternalStore(
    store.subscribe,
    store.getSnapshot,
    store.getSnapshot
  );
  return {
    ...snapshot,
    identity,
    update: store.update,
    flush: store.flush,
    retry: store.retry,
  };
}
