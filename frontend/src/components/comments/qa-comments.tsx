"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useOrganization } from "@clerk/nextjs";
import {
  ClientSideSuspense,
  RoomProvider,
  useThreads,
} from "@liveblocks/react/suspense";
import { Composer, Thread } from "@liveblocks/react-ui";
import { MessageSquare, MessageSquarePlus, X } from "lucide-react";
import {
  formatLineRange,
  lineRangesEqual,
  type LineRange,
} from "@/lib/line-range";
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
    // Keyed by file as well as org: the boundary has no way to un-fail
    // itself, so without this a single bad render would hide comments on
    // every other file until an org switch or a full reload.
    <CommentsErrorBoundary
      key={`${organization.id}:${props.taskId}:${props.filePath}`}
    >
      <RoomProvider id={`qa:${organization.id}:${props.taskId}`}>
        <ClientSideSuspense fallback={null}>
          <OverlayInner {...props} />
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
  // Threads without a line anchor (legacy whole-file comments) pin to line
  // 1 so they stay reachable — the inline overlay is the only comments UI.
  const byLine = useMemo(() => {
    const map = new Map<number, typeof threads>();
    for (const thread of threads) {
      const line = thread.metadata.lineStart ?? 1;
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

  // A notification deep link arrives with the lines selected but nothing
  // open, so the overlay would offer to start a *second* thread on lines
  // that already have one. Open the existing thread instead. Once per file
  // (this subtree remounts per file), so closing it doesn't reopen it.
  const autoOpenedRef = useRef(false);
  useEffect(() => {
    if (autoOpenedRef.current || !selectedLines) return;
    for (const [line, group] of byLine) {
      if (line !== selectedLines.start) continue;
      // Any one thread on this line matching is enough. Comparing against
      // the group's widest end would miss a link to the shorter of two
      // threads that share a start line.
      const hit = group.some(
        (t) => (t.metadata.lineEnd ?? line) === selectedLines.end
      );
      if (hit) {
        autoOpenedRef.current = true;
        setOpenLine(line);
        return;
      }
    }
  }, [byLine, selectedLines]);

  const openThreads = openLine != null ? (byLine.get(openLine) ?? []) : [];
  const openBottom =
    openThreads.length > 0
      ? Math.max(...openThreads.map((t) => t.metadata.lineEnd ?? openLine!))
      : null;

  const groupRange = (line: number, group: typeof threads): LineRange => ({
    start: line,
    end: Math.max(...group.map((t) => t.metadata.lineEnd ?? line)),
  });

  // Closing a thread retires the highlight it created — otherwise the
  // lingering selection sprouts a new-comment bubble the moment the thread
  // dismisses. A selection the user made themselves in the meantime
  // (different range) is left alone.
  const closeThread = () => {
    if (
      openLine != null &&
      openThreads.length > 0 &&
      lineRangesEqual(selectedLines, groupRange(openLine, openThreads))
    ) {
      onSelectLines?.(null);
    }
    setOpenLine(null);
  };

  return (
    <div className="pointer-events-none absolute inset-0 z-10">
      {[...byLine.entries()].map(([line, group]) => (
        <button
          key={line}
          type="button"
          onClick={() => {
            if (openLine === line) {
              closeThread();
              return;
            }
            // Opening a thread highlights the lines it addresses — the
            // same selection mechanism the URL anchor drives.
            setOpenLine(line);
            onSelectLines?.(groupRange(line, group));
          }}
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
              onClick={closeThread}
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

      {/* Hidden while a thread popover is open — its highlight would
          otherwise sprout a redundant new-comment bubble. */}
      {selectedLines && openLine == null && (
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
 * Every comment thread on the task, for the Task Overview tab — grouped by
 * file and ordered by line, with a chip that jumps to the file at the
 * thread's lines. Renders nothing while the task has no comments.
 */
export function TaskCommentsOverview({
  taskId,
  onOpenFileAtLines,
}: {
  taskId: string;
  onOpenFileAtLines?: (filePath: string, lines: LineRange | null) => void;
}) {
  const { organization } = useOrganization();
  if (!organization) return null;
  return (
    <CommentsErrorBoundary key={`${organization.id}:${taskId}`}>
      <RoomProvider id={`qa:${organization.id}:${taskId}`}>
        <ClientSideSuspense fallback={null}>
          <CommentsOverviewInner onOpenFileAtLines={onOpenFileAtLines} />
        </ClientSideSuspense>
      </RoomProvider>
    </CommentsErrorBoundary>
  );
}

function CommentsOverviewInner({
  onOpenFileAtLines,
}: {
  onOpenFileAtLines?: (filePath: string, lines: LineRange | null) => void;
}) {
  const { threads } = useThreads();
  const sorted = useMemo(
    () =>
      [...threads].sort(
        (a, b) =>
          a.metadata.filePath.localeCompare(b.metadata.filePath) ||
          (a.metadata.lineStart ?? 0) - (b.metadata.lineStart ?? 0)
      ),
    [threads]
  );

  if (sorted.length === 0) return null;

  return (
    <div className="flex flex-col gap-3 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-muted-foreground font-mono text-[11px] font-semibold tracking-wider uppercase">
          Comments
        </h2>
        <span className="text-muted-foreground font-mono text-[11px]">
          {sorted.length} thread{sorted.length === 1 ? "" : "s"}
        </span>
      </div>
      <div className="space-y-2">
        {sorted.map((thread) => {
          const lines =
            thread.metadata.lineStart != null && thread.metadata.lineEnd != null
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
              <button
                type="button"
                onClick={() =>
                  onOpenFileAtLines?.(thread.metadata.filePath, lines)
                }
                className="text-primary bg-primary/10 hover:bg-primary/20 mt-2 ml-2 rounded px-1.5 py-0.5 font-mono text-[10px] transition-colors"
                title="Open file at these lines"
              >
                {thread.metadata.filePath}
                {lines ? ` ${formatLineRange(lines)}` : ""}
              </button>
              <Thread thread={thread} />
            </div>
          );
        })}
      </div>
    </div>
  );
}
