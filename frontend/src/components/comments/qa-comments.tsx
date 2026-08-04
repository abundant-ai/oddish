"use client";

import { useMemo, useState } from "react";
import { useOrganization } from "@clerk/nextjs";
import {
  ClientSideSuspense,
  RoomProvider,
  useThreads,
} from "@liveblocks/react/suspense";
import { Composer, Thread } from "@liveblocks/react-ui";
import { ChevronDown, ChevronRight, MessageSquare } from "lucide-react";
import { formatLineRange, type LineRange } from "@/lib/line-range";
import { Badge } from "@/components/ui/badge";
import { CommentsErrorBoundary } from "@/components/comments/comments-provider";

/**
 * Liveblocks-backed review comments for the file open in the task files
 * pane. Each task is one org-scoped room (`qa:{orgId}:{taskId}`); threads
 * carry the file/line anchor as metadata, and the composer binds to the
 * pane's current line selection (the same selection the URL carries as
 * ``?taskLines=``). Mentions, editing, resolving, reactions, and
 * notifications all come from Liveblocks.
 */
export function QaCommentsSection(props: {
  taskId: string;
  filePath: string;
  selectedLines: LineRange | null;
  onSelectLines?: (range: LineRange | null) => void;
}) {
  const { organization } = useOrganization();
  if (!organization) return null;
  return (
    <CommentsErrorBoundary>
      <RoomProvider id={`qa:${organization.id}:${props.taskId}`}>
        <ClientSideSuspense fallback={null}>
          {/* Keyed by file so a collapse choice on one file doesn't stick
              to the next — each file re-decides from its own thread count. */}
          <FileThreads key={props.filePath} {...props} />
        </ClientSideSuspense>
      </RoomProvider>
    </CommentsErrorBoundary>
  );
}

function FileThreads({
  filePath,
  selectedLines,
  onSelectLines,
}: {
  taskId: string;
  filePath: string;
  selectedLines: LineRange | null;
  onSelectLines?: (range: LineRange | null) => void;
}) {
  const { threads } = useThreads({ query: { metadata: { filePath } } });
  const sorted = useMemo(
    () =>
      [...threads].sort(
        (a, b) => (a.metadata.lineStart ?? 0) - (b.metadata.lineStart ?? 0)
      ),
    [threads]
  );

  // No explicit choice yet → open when the file already has threads.
  const [collapsed, setCollapsed] = useState<boolean | null>(null);
  const isOpen = collapsed === null ? sorted.length > 0 : !collapsed;

  return (
    <div className="border-border bg-card/50 border-t">
      <button
        type="button"
        onClick={() => setCollapsed(isOpen)}
        className="text-muted-foreground hover:text-foreground flex w-full items-center gap-2 px-4 py-2 text-xs font-medium transition-colors"
      >
        {isOpen ? (
          <ChevronDown className="h-3 w-3" />
        ) : (
          <ChevronRight className="h-3 w-3" />
        )}
        <MessageSquare className="h-3.5 w-3.5" />
        Comments
        {sorted.length > 0 && (
          <Badge variant="secondary" className="px-1.5 text-[10px]">
            {sorted.length}
          </Badge>
        )}
        {!isOpen && selectedLines && (
          <span className="text-primary ml-auto font-mono text-[10px]">
            comment on {formatLineRange(selectedLines)}
          </span>
        )}
      </button>

      {isOpen && (
        <div className="max-h-80 space-y-2 overflow-y-auto px-3 pb-3">
          {sorted.map((thread) => {
            const lines =
              thread.metadata.lineStart != null &&
              thread.metadata.lineEnd != null
                ? {
                    start: thread.metadata.lineStart,
                    end: thread.metadata.lineEnd,
                  }
                : null;
            return (
              <div
                key={thread.id}
                className="border-border overflow-hidden rounded-lg border"
              >
                {lines && (
                  <button
                    type="button"
                    onClick={() => onSelectLines?.(lines)}
                    className="text-primary bg-primary/10 hover:bg-primary/20 mt-2 ml-2 rounded px-1.5 py-0.5 font-mono text-[10px] transition-colors"
                    title="Jump to lines"
                  >
                    {formatLineRange(lines)}
                  </button>
                )}
                <Thread thread={thread} />
              </div>
            );
          })}

          <div className="space-y-1">
            <div className="text-muted-foreground px-1 text-[11px]">
              {selectedLines ? (
                <>
                  Commenting on{" "}
                  <span className="text-primary font-mono">
                    {formatLineRange(selectedLines)}
                  </span>
                </>
              ) : (
                <>Commenting on this file — select lines to anchor</>
              )}
            </div>
            <Composer
              className="border-border rounded-lg border"
              metadata={{
                filePath,
                ...(selectedLines && {
                  lineStart: selectedLines.start,
                  lineEnd: selectedLines.end,
                }),
              }}
            />
          </div>
        </div>
      )}
    </div>
  );
}
