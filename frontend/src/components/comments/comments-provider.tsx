"use client";

import { Component, type ReactNode } from "react";
import { LiveblocksProvider } from "@liveblocks/react/suspense";
import "@liveblocks/react-ui/styles.css";
import "@liveblocks/react-ui/styles/dark/attributes.css";

// Never resolve a user to undefined: suspense-mode user hooks treat an
// unresolvable user as an error, which would trip the boundary below and
// unmount the whole comment UI over one missing profile. A stable "Unknown"
// entry keeps every author renderable.
const UNKNOWN_USER = { name: "Unknown" };

/**
 * Liveblocks client for QA comments, mounted once around the signed-in app
 * shell. Auth (`/api/liveblocks-auth`) grants only the caller's org's rooms;
 * user display info and @mention suggestions resolve through Clerk via
 * `/api/liveblocks-users`.
 */
export function CommentsProvider({ children }: { children: ReactNode }) {
  return (
    <LiveblocksProvider
      authEndpoint="/api/liveblocks-auth"
      resolveUsers={async ({ userIds }) => {
        const res = await fetch(
          `/api/liveblocks-users?ids=${encodeURIComponent(userIds.join(","))}`,
        );
        // The result must align 1:1 with userIds — a short/empty array would
        // mis-map authors.
        if (!res.ok) return userIds.map(() => UNKNOWN_USER);
        const users = (await res.json()) as (
          | { name: string; avatar?: string }
          | null
          | undefined
        )[];
        return userIds.map((_, index) => users[index] ?? UNKNOWN_USER);
      }}
      resolveMentionSuggestions={async ({ text }) => {
        const res = await fetch(
          `/api/liveblocks-users?query=${encodeURIComponent(text ?? "")}`,
        );
        return res.ok ? await res.json() : [];
      }}
    >
      {children}
    </LiveblocksProvider>
  );
}

/**
 * Renders `fallback` (default: nothing) when Liveblocks is unreachable or
 * unconfigured (no LIVEBLOCKS_SECRET_KEY) instead of breaking the page
 * around it.
 */
export class CommentsErrorBoundary extends Component<
  { children: ReactNode; fallback?: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: unknown) {
    // The UI degrades on failure by design, so leave a trace for whoever is
    // wondering why the comment UI isn't there (missing/mis-scoped
    // LIVEBLOCKS_SECRET_KEY is the usual answer).
    console.warn("[qa-comments] comment UI hidden due to error:", error);
  }

  render() {
    return this.state.failed ? (this.props.fallback ?? null) : this.props.children;
  }
}
