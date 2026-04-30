"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { streamCCChatMessage } from "@/lib/cc-chat-stream";
import { renderStreamEvent } from "@/lib/cc-chat-render";

type Phase = "creating" | "ready" | "thinking" | "idle" | "closed" | "error";

type ChatTurn =
  | { role: "user"; text: string }
  | { role: "assistant"; events: unknown[] };

type RenderItem =
  | { kind: "event"; event: unknown }
  | { kind: "system_run"; count: number };

// Coalesce consecutive `system` events (excluding init, which the renderer
// already drops) into a single "↻ N system updates" line so claude's
// task_progress chatter doesn't flood the panel.
function collapseSystemRuns(events: unknown[]): RenderItem[] {
  const out: RenderItem[] = [];
  let run = 0;
  for (const e of events) {
    const obj = e as { type?: unknown; subtype?: unknown } | null;
    if (
      obj &&
      typeof obj === "object" &&
      obj.type === "system" &&
      obj.subtype !== "init"
    ) {
      run++;
      continue;
    }
    if (run > 0) {
      out.push({ kind: "system_run", count: run });
      run = 0;
    }
    out.push({ kind: "event", event: e });
  }
  if (run > 0) out.push({ kind: "system_run", count: run });
  return out;
}

export type CCChatScope =
  | { kind: "experiment"; experimentId: string }
  | { kind: "task"; taskId: string };

function scopeBaseUrl(scope: CCChatScope): string {
  return scope.kind === "experiment"
    ? `/api/experiments/${encodeURIComponent(scope.experimentId)}/cc-session`
    : `/api/tasks/${encodeURIComponent(scope.taskId)}/cc-session`;
}

export function CCChatModal({
  experimentId,
  scope: scopeProp,
  open,
  onOpenChange,
}: {
  /** @deprecated pass `scope={{kind:"experiment",experimentId}}` instead. */
  experimentId?: string;
  scope?: CCChatScope;
  open: boolean;
  onOpenChange: (next: boolean) => void;
}) {
  const scope: CCChatScope =
    scopeProp ??
    (experimentId
      ? { kind: "experiment", experimentId }
      : { kind: "experiment", experimentId: "" });
  const baseUrl = scopeBaseUrl(scope);

  const [phase, setPhase] = useState<Phase>("creating");
  const [error, setError] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [draft, setDraft] = useState("");
  const sendingRef = useRef(false);

  const start = useCallback(async () => {
    setPhase("creating");
    setError(null);
    try {
      const res = await fetch(baseUrl, { method: "POST" });
      if (!res.ok) throw new Error(`start ${res.status}`);
      const data = (await res.json()) as { session_id: string };
      setSessionId(data.session_id);
      setPhase("ready");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setPhase("error");
    }
  }, [baseUrl]);

  useEffect(() => {
    if (!open) return;
    void start();
  }, [open, start]);

  // Close on unmount, modal close, OR tab/window close.
  // - useEffect cleanup handles in-app navigation and modal close.
  // - pagehide handles tab close, window close, and iOS BFCache eviction —
  //   those don't run React unmount.
  useEffect(() => {
    if (!sessionId) return;
    const url = `${baseUrl}/${encodeURIComponent(sessionId)}`;
    const closeSession = () => {
      // sendBeacon is POST-only; fetch keepalive lets DELETE survive unload.
      void fetch(url, { method: "DELETE", keepalive: true });
    };
    window.addEventListener("pagehide", closeSession);
    return () => {
      window.removeEventListener("pagehide", closeSession);
      closeSession();
    };
  }, [baseUrl, sessionId]);

  const send = useCallback(async () => {
    if (!sessionId || sendingRef.current || !draft.trim()) return;
    sendingRef.current = true;
    const content = draft;
    setDraft("");
    setTurns((prev) => [
      ...prev,
      { role: "user", text: content },
      { role: "assistant", events: [] },
    ]);
    setPhase("thinking");

    const url = `${baseUrl}/${encodeURIComponent(sessionId)}/messages`;

    try {
      await streamCCChatMessage(url, { content }, (e) => {
        if (e.kind === "message" || e.kind === "error") {
          // Pure update: in React strict mode the updater runs twice, so
          // mutating `last.events` would append the same event twice.
          setTurns((prev) => {
            if (prev.length === 0) return prev;
            const last = prev[prev.length - 1];
            if (last.role !== "assistant") return prev;
            const newLast = {
              ...last,
              events: [...last.events, e.data],
            };
            return [...prev.slice(0, -1), newLast];
          });
        }
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setPhase("error");
    } finally {
      sendingRef.current = false;
      setPhase("idle");
    }
  }, [baseUrl, draft, sessionId]);

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="w-full sm:max-w-xl flex flex-col gap-4"
      >
        <SheetHeader>
          <SheetTitle>Chat with experiment logs</SheetTitle>
          <SheetDescription>
            <span className="inline-flex items-center gap-1.5">
              {(phase === "creating" || phase === "thinking") && (
                <span
                  aria-hidden
                  className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-muted-foreground/30 border-t-muted-foreground"
                />
              )}
              Status: <span className="font-mono text-xs">{phase}</span>
              {phase === "creating" && (
                <span className="text-xs opacity-60">
                  (booting sandbox + uploading trial logs)
                </span>
              )}
              {error ? (
                <span className="text-destructive ml-2">— {error}</span>
              ) : null}
            </span>
          </SheetDescription>
        </SheetHeader>

        <div className="flex-1 overflow-y-auto space-y-4 py-2">
          {turns.map((turn, i) => (
            <div key={i}>
              {turn.role === "user" ? (
                <div className="rounded-md border bg-muted/50 px-3 py-2">
                  <div className="text-xs font-medium uppercase opacity-60">
                    you
                  </div>
                  <div className="whitespace-pre-wrap text-sm">{turn.text}</div>
                </div>
              ) : (
                <div className="rounded-md border px-3 py-2 space-y-2">
                  <div className="text-xs font-medium uppercase opacity-60">
                    claude
                  </div>
                  {turn.events.length === 0 ? (
                    <div className="text-sm italic opacity-60">thinking…</div>
                  ) : (
                    <div className="space-y-1">
                      {collapseSystemRuns(turn.events).map((item, j) =>
                        item.kind === "system_run" ? (
                          <div
                            key={j}
                            className="text-xs opacity-50 italic"
                          >
                            ↻ {item.count} system updates
                          </div>
                        ) : (
                          (() => {
                            const rendered = renderStreamEvent(item.event);
                            if (rendered === null) return null;
                            return (
                              <div key={j} className="text-sm">
                                {rendered}
                              </div>
                            );
                          })()
                        ),
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>

        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            void send();
          }}
        >
          <Input
            placeholder="Ask about this experiment…"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            disabled={phase === "thinking" || phase === "creating" || phase === "error"}
          />
          <Button
            type="submit"
            disabled={phase === "thinking" || phase === "creating" || phase === "error" || !draft.trim()}
          >
            Send
          </Button>
        </form>

        <div className="flex justify-end pt-2">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={!sessionId || phase === "creating" || phase === "error"}
            onClick={() => {
              if (!sessionId) return;
              const url = `${baseUrl}/${encodeURIComponent(sessionId)}/skills.tar.gz`;
              window.location.href = url;
            }}
          >
            Download skills
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  );
}
