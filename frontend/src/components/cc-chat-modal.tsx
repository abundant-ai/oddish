"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { CodeBlock } from "@/components/code-block";
import { streamCCChatMessage } from "@/lib/cc-chat-stream";

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

  // Close on unmount / open=false
  useEffect(() => {
    return () => {
      if (sessionId) {
        const url = `/api/experiments/${encodeURIComponent(
          experimentId,
        )}/cc-session/${encodeURIComponent(sessionId)}`;
        if (typeof navigator !== "undefined" && navigator.sendBeacon) {
          navigator.sendBeacon(url);
        } else {
          void fetch(url, { method: "DELETE", keepalive: true });
        }
      }
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
          setTurns((prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last && last.role === "assistant") {
              last.events = [...last.events, e.data];
            }
            return next;
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
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Chat with experiment logs</DialogTitle>
          <DialogDescription>
            Status: <span className="font-mono text-xs">{phase}</span>
            {error ? (
              <span className="text-destructive ml-2">— {error}</span>
            ) : null}
          </DialogDescription>
        </DialogHeader>

        <div className="max-h-[60vh] overflow-y-auto space-y-4 py-2">
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
                <div className="rounded-md border px-3 py-2">
                  <div className="text-xs font-medium uppercase opacity-60">
                    claude
                  </div>
                  {turn.events.length === 0 ? (
                    <div className="text-sm italic opacity-60">thinking…</div>
                  ) : (
                    <CodeBlock
                      code={turn.events
                        .map((e) => JSON.stringify(e, null, 2))
                        .join("\n")}
                      language="json"
                    />
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
      </DialogContent>
    </Dialog>
  );
}
