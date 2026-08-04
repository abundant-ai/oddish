"use client";

import { Component, type ReactNode } from "react";
import { LiveblocksProvider } from "@liveblocks/react/suspense";
import "@liveblocks/react-ui/styles.css";
import "@liveblocks/react-ui/styles/dark/attributes.css";

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
        return res.ok ? await res.json() : [];
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
 * Hides comment UI entirely when Liveblocks is unreachable or unconfigured
 * (no LIVEBLOCKS_SECRET_KEY) instead of breaking the page around it.
 */
export class CommentsErrorBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    return this.state.failed ? null : this.props.children;
  }
}
