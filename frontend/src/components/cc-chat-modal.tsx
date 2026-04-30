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

export function CCChatModal({
  experimentId,
  open,
  onOpenChange,
}: {
  experimentId: string;
  open: boolean;
  onOpenChange: (next: boolean) => void;
}) {
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
      const res = await fetch(
        `/api/experiments/${encodeURIComponent(experimentId)}/cc-session`,
        { method: "POST" },
      );
      if (!res.ok) throw new Error(`start ${res.status}`);
      const data = (await res.json()) as { session_id: string };
      setSessionId(data.session_id);
      setPhase("ready");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setPhase("error");
    }
  }, [experimentId]);

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
    const url = `/api/experiments/${encodeURIComponent(
      experimentId,
    )}/cc-session/${encodeURIComponent(sessionId)}`;
    const closeSession = () => {
      // sendBeacon is POST-only; fetch keepalive lets DELETE survive unload.
      void fetch(url, { method: "DELETE", keepalive: true });
    };
    window.addEventListener("pagehide", closeSession);
    return () => {
      window.removeEventListener("pagehide", closeSession);
      closeSession();
    };
  }, [experimentId, sessionId]);

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

    const url = `/api/experiments/${encodeURIComponent(
      experimentId,
    )}/cc-session/${encodeURIComponent(sessionId)}/messages`;

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
  }, [draft, experimentId, sessionId]);

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="w-full sm:max-w-xl flex flex-col gap-4"
      >
        <SheetHeader>
          <SheetTitle>Chat with experiment logs</SheetTitle>
          <SheetDescription>
            Status: <span className="font-mono text-xs">{phase}</span>
            {error ? (
              <span className="text-destructive ml-2">— {error}</span>
            ) : null}
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
                      {turn.events.map((event, j) => {
                        const rendered = renderStreamEvent(event);
                        if (rendered === null) return null;
                        return (
                          <div key={j} className="text-sm">
                            {rendered}
                          </div>
                        );
                      })}
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
              const url = `/api/experiments/${encodeURIComponent(
                experimentId,
              )}/cc-session/${encodeURIComponent(sessionId)}/skills.tar.gz`;
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
