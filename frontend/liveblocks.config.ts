/**
 * Global Liveblocks types for QA comments.
 *
 * Rooms are org-scoped by id — `qa:{clerkOrgId}:{taskId}` — and the auth
 * route only ever grants `qa:{callerOrgId}:*`, so org separation is enforced
 * at token mint, not in UI code. Threads carry the file/line anchor as
 * metadata (the same `?taskFile=`/`?taskLines=` anchors the task pages
 * address in the URL).
 */
declare global {
  interface Liveblocks {
    UserMeta: {
      id: string; // Clerk user id
      info: {
        name: string;
        avatar?: string;
      };
    };
    ThreadMetadata: {
      filePath: string;
      lineStart?: number;
      lineEnd?: number;
    };
  }
}

export {};
