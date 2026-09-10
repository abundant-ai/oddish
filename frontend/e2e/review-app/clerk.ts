import { useSyncExternalStore } from "react";

// Separate local fixture app; production authentication is unchanged.
let identity = {
  orgRole: "org:admin",
  userId: "maya",
  orgId: "fixture",
  isLoaded: false,
};
const listeners = new Set<() => void>();
const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
};
export function setFixtureAccount(userId: string, orgId: string) {
  identity = { orgRole: "org:admin", userId, orgId, isLoaded: true };
  listeners.forEach((listener) => listener());
}
export const useAuth = () =>
  useSyncExternalStore(
    subscribe,
    () => identity,
    () => identity
  );
export const useOrganization = () => ({ organization: null, isLoaded: true });
