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
import { formatLineRange, type LineRange } from "@/lib/line-range";
import { CommentsErrorBoundary } from "@/components/comments/comments-provider";

/** Pierre's fixed row height (--diffs-line-height); anchors overlay rows. */
const LINE_REM = 1.25;

/**
 * Pierre pads the code block above line 1 (--diffs-gap-block, pinned in
 * pierre-options.ts). The overlay is positioned against the wrapper around
 * the whole pierre element, so every offset carries this head start.
 */
const CODE_TOP_REM = 0.5;

const remTop = (line: number) => `${CODE_TOP_REM + (line - 1) * LINE_REM}rem`;
const remBelow = (line: number) => `${CODE_TOP_REM + line * LINE_REM}rem`;

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
  /** Lines actually rendered. Large files arrive truncated, and a marker
   *  placed past the last row would stretch the pane's scroll height by
   *  the whole missing tail. Omit when the file is whole. */
  lineCount?: number;
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
  lineCount,
}: {
  taskId: string;
  filePath: string;
  selectedLines: LineRange | null;
  onSelectLines?: (range: LineRange | null) => void;
  lineCount?: number;
}) {
  const { threads } = useThreads({ query: { metadata: { filePath } } });

  /**
   * The last row that exists on screen. A large file arrives truncated, and
   * an absolutely positioned marker extends its scroll container, so drawing
   * at line 400 of a 40-line render adds ~7,400px of blank space to scroll
   * through.
   *
   * `pin` is therefore applied at exactly one kind of place: computing a
   * `top`. Every value — thread anchors, group ranges, the selection this
   * component reports back for the URL, what a new thread records — stays
   * the true line number. Pinning a value instead of a position is what put
   * the wrong lines in shared links.
   */
  const maxLine = lineCount && lineCount > 0 ? lineCount : Infinity;
  const pin = (line: number) => Math.min(line, maxLine);

  // One marker per anchor start line; a line can carry several threads.
  // Threads without a line anchor (legacy whole-file comments) sit on line 1
  // so they stay reachable — the inline overlay is the only comments UI.
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

  // The lines a click on this line's bubble highlights: the union, so every
  // commented line lights up when several threads share a start.
  const groupRange = (line: number, group: typeof threads): LineRange => ({
    start: line,
    end: Math.max(...group.map((t) => t.metadata.lineEnd ?? line)),
  });

  /** Threads in this group that sit past the last rendered line. */
  const beyondCount = (group: typeof threads) =>
    group.filter((t) => (t.metadata.lineStart ?? 1) > maxLine).length;

  // Several groups past the truncation all draw on the last row. Fan them
  // out sideways so a later one doesn't hide an earlier one — presentation
  // only, recomputed each render, no bearing on which lines they address.
  const drawnRows = [...byLine.keys()].sort((a, b) => a - b);
  const stackOffset = (line: number) =>
    drawnRows.filter((other) => other < line && pin(other) === pin(line))
      .length;

  /**
   * Is `range` this group's highlight? True for the union we set on click,
   * and for any single thread's own range — a deep link addresses one
   * thread, which may be narrower than the union. Both open and close go
   * through this, so they can't disagree about what belongs to a group.
   */
  const isGroupRange = (
    line: number,
    group: typeof threads,
    range: LineRange | null
  ): boolean => {
    if (!range || range.start !== line) return false;
    if (range.end === groupRange(line, group).end) return true;
    return group.some((t) => (t.metadata.lineEnd ?? line) === range.end);
  };

  /**
   * The thread group on screen — a view of the current selection, not state
   * of its own. Two things follow, and both used to be bugs:
   *
   * - Its threads must still exist. Delete the last one and this empties,
   *   so the new-comment bubble comes back without an effect to notice.
   * - The selection must still name it. Select other lines and the popover
   *   steps aside for the composer, instead of blocking new comments until
   *   the reader closes it by hand.
   *
   * A null selection keeps it open: nothing else is competing for the
   * space, and the overlay may be mounted without selection wiring at all.
   */
  const openGroup = (() => {
    if (openLine == null) return null;
    const group = byLine.get(openLine);
    if (!group) return null;
    if (selectedLines && !isGroupRange(openLine, group, selectedLines)) {
      return null;
    }
    return { line: openLine, threads: group };
  })();

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
      if (isGroupRange(line, group, selectedLines)) {
        autoOpenedRef.current = true;
        setOpenLine(line);
        return;
      }
    }
    // isGroupRange is pure over its arguments; byLine and the selection are
    // the only inputs that can change it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [byLine, selectedLines]);

  // Closing retires the highlight along with the thread. `openGroup` only
  // exists while the selection names it, so any selection standing here is
  // this thread's — no need to ask again whether it belongs.
  const closeThread = () => {
    if (openGroup && selectedLines) onSelectLines?.(null);
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
          style={{
            top: remTop(pin(line)),
            left: `${0.125 + stackOffset(line) * 0.85}rem`,
          }}
          className="bg-primary text-primary-foreground pointer-events-auto absolute flex h-[1.1rem] min-w-[1.1rem] items-center justify-center gap-0.5 rounded-full px-0.5 text-[9px] font-semibold shadow-sm transition-transform hover:scale-110"
          title={
            beyondCount(group) > 0
              ? `${group.length} comment thread${group.length === 1 ? "" : "s"} (${beyondCount(group)} on lines past the part of this file that is loaded)`
              : `${group.length} comment thread${group.length === 1 ? "" : "s"}`
          }
        >
          <MessageSquare className="h-2.5 w-2.5" />
          {group.length > 1 && group.length}
        </button>
      ))}

      {openGroup && (
        <div
          // Sits under the last line any thread in the group addresses —
          // the same range the header names and the highlight covers, drawn
          // no lower than the file's last rendered row.
          style={{
            top: remBelow(
              pin(groupRange(openGroup.line, openGroup.threads).end)
            ),
          }}
          className="border-border bg-background pointer-events-auto absolute left-12 w-[min(28rem,80%)] rounded-lg border shadow-xl"
        >
          <div className="border-border flex items-center justify-between border-b px-3 py-1.5">
            <span className="text-muted-foreground font-mono text-[10px]">
              {formatLineRange(groupRange(openGroup.line, openGroup.threads))}
              {beyondCount(openGroup.threads) > 0 && (
                <span className="ml-2 normal-case">
                  · {beyondCount(openGroup.threads)} below the loaded part
                </span>
              )}
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
            {openGroup.threads.map((thread) => (
              <Thread key={thread.id} thread={thread} />
            ))}
          </div>
        </div>
      )}

      {/* The selection either has a conversation on it or is somewhere to
          start one — never both. `openGroup` is the same derived answer the
          popover above uses, so the two can't appear together. */}
      {selectedLines && !openGroup && (
        <button
          type="button"
          onClick={() => setComposerOpen((prev) => !prev)}
          style={{ top: remTop(pin(selectedLines.start)) }}
          className="bg-primary text-primary-foreground pointer-events-auto absolute left-6 flex h-[1.1rem] w-[1.1rem] items-center justify-center rounded-full shadow-sm transition-transform hover:scale-110"
          title={`Comment on ${formatLineRange(selectedLines)}`}
        >
          <MessageSquarePlus className="h-2.5 w-2.5" />
        </button>
      )}

      {selectedLines && composerOpen && !openGroup && (
        <div
          style={{ top: remBelow(pin(selectedLines.end)) }}
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
              // Open the thread being created rather than clearing the
              // selection: otherwise the composer just vanishes and the new
              // conversation sits closed behind a bubble, which reads as a
              // failed submit. `openGroup` is derived, so this waits for the
              // thread to show up in `useThreads` and undoes itself if the
              // create is rolled back.
              setOpenLine(selectedLines.start);
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
