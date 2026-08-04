"use client";

import { useEffect, useMemo, useState } from "react";
import { useOrganization } from "@clerk/nextjs";
import {
  ClientSideSuspense,
  RoomProvider,
  useThreads,
} from "@liveblocks/react/suspense";
import { Composer, Thread } from "@liveblocks/react-ui";
import {
  ChevronDown,
  ChevronRight,
  MessageSquare,
  MessageSquarePlus,
  X,
} from "lucide-react";
import { formatLineRange, type LineRange } from "@/lib/line-range";
import { Badge } from "@/components/ui/badge";
import { CommentsErrorBoundary } from "@/components/comments/comments-provider";

/** Pierre's fixed row height (--diffs-line-height); anchors overlay rows. */
const LINE_REM = 1.25;

const remTop = (line: number) => `${(line - 1) * LINE_REM}rem`;
const remBelow = (line: number) => `${line * LINE_REM}rem`;

/**
 * GitHub-style inline comment layer for the code view. Rendered inside the
 * code renderer's content-sized wrapper (see CodeRenderer.lineOverlay), so
 * a marker pinned at a line's offset scrolls with the code. Each line that
 * has threads gets a gutter bubble; clicking it opens the threads right
 * below the line. Selecting lines shows a "new comment" bubble that opens
 * a composer anchored to the selection.
 */
export function InlineCommentOverlay(props: {
  taskId: string;
  filePath: string;
  selectedLines: LineRange | null;
  onSelectLines?: (range: LineRange | null) => void;
}) {
  const { organization } = useOrganization();
  if (!organization) return null;
  return (
    <CommentsErrorBoundary key={organization.id}>
      <RoomProvider id={`qa:${organization.id}:${props.taskId}`}>
        <ClientSideSuspense fallback={null}>
          <OverlayInner key={props.filePath} {...props} />
        </ClientSideSuspense>
      </RoomProvider>
    </CommentsErrorBoundary>
  );
}

function OverlayInner({
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

  // One marker per anchor start line; a line can carry several threads.
  const byLine = useMemo(() => {
    const map = new Map<number, typeof threads>();
    for (const thread of threads) {
      const line = thread.metadata.lineStart;
      if (line == null) continue;
      map.set(line, [...(map.get(line) ?? []), thread]);
    }
    return map;
  }, [threads]);

  const [openLine, setOpenLine] = useState<number | null>(null);
  const [composerOpen, setComposerOpen] = useState(false);

  // A new selection is a new comment target; a cleared one retires the
  // composer with it.
  useEffect(() => {
    setComposerOpen(false);
  }, [selectedLines?.start, selectedLines?.end]);

  const openThreads = openLine != null ? (byLine.get(openLine) ?? []) : [];
  const openBottom =
    openThreads.length > 0
      ? Math.max(...openThreads.map((t) => t.metadata.lineEnd ?? openLine!))
      : null;

  return (
    <div className="pointer-events-none absolute inset-0 z-10">
      {[...byLine.entries()].map(([line, group]) => (
        <button
          key={line}
          type="button"
          onClick={() => setOpenLine((prev) => (prev === line ? null : line))}
          style={{ top: remTop(line) }}
          className="bg-primary text-primary-foreground pointer-events-auto absolute left-0.5 flex h-[1.1rem] min-w-[1.1rem] items-center justify-center gap-0.5 rounded-full px-0.5 text-[9px] font-semibold shadow-sm transition-transform hover:scale-110"
          title={`${group.length} comment thread${group.length === 1 ? "" : "s"}`}
        >
          <MessageSquare className="h-2.5 w-2.5" />
          {group.length > 1 && group.length}
        </button>
      ))}

      {openLine != null && openThreads.length > 0 && (
        <div
          style={{ top: remBelow(openBottom ?? openLine) }}
          className="border-border bg-background pointer-events-auto absolute left-12 w-[min(28rem,80%)] rounded-lg border shadow-xl"
        >
          <div className="border-border flex items-center justify-between border-b px-3 py-1.5">
            <span className="text-muted-foreground font-mono text-[10px]">
              {openThreads[0].metadata.lineEnd != null &&
              openThreads[0].metadata.lineStart != null
                ? formatLineRange({
                    start: openThreads[0].metadata.lineStart,
                    end: openThreads[0].metadata.lineEnd,
                  })
                : `L${openLine}`}
            </span>
            <button
              type="button"
              onClick={() => setOpenLine(null)}
              className="text-muted-foreground hover:text-foreground"
              aria-label="Close thread"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
          <div className="max-h-80 overflow-y-auto">
            {openThreads.map((thread) => (
              <Thread key={thread.id} thread={thread} />
            ))}
          </div>
        </div>
      )}

      {selectedLines && (
        <button
          type="button"
          onClick={() => setComposerOpen((prev) => !prev)}
          style={{ top: remTop(selectedLines.start) }}
          className="bg-primary text-primary-foreground pointer-events-auto absolute left-6 flex h-[1.1rem] w-[1.1rem] items-center justify-center rounded-full shadow-sm transition-transform hover:scale-110"
          title={`Comment on ${formatLineRange(selectedLines)}`}
        >
          <MessageSquarePlus className="h-2.5 w-2.5" />
        </button>
      )}

      {selectedLines && composerOpen && (
        <div
          style={{ top: remBelow(selectedLines.end) }}
          className="border-border bg-background pointer-events-auto absolute left-12 w-[min(28rem,80%)] rounded-lg border shadow-xl"
        >
          <div className="border-border flex items-center justify-between border-b px-3 py-1.5">
            <span className="text-muted-foreground font-mono text-[10px]">
              New comment on {formatLineRange(selectedLines)}
            </span>
            <button
              type="button"
              onClick={() => setComposerOpen(false)}
              className="text-muted-foreground hover:text-foreground"
              aria-label="Close composer"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
          <Composer
            autoFocus
            metadata={{
              filePath,
              lineStart: selectedLines.start,
              lineEnd: selectedLines.end,
            }}
            onComposerSubmit={() => {
              setComposerOpen(false);
              onSelectLines?.(null);
            }}
          />
        </div>
      )}
    </div>
  );
}

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
    <CommentsErrorBoundary key={organization.id}>
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
