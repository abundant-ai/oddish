"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { ChatBubble, ChatScopeKind } from "@/lib/cc-chat-types";

interface UseChatSession {
  sessionId: string | null;
  bubbles: ChatBubble[];
  working: boolean;
  error: string | null;
  unavailable: boolean; // backend 503
  send: (text: string) => Promise<void>;
  reset: () => void;            // "New chat"
  adopt: (id: string) => void;  // jump into an existing session id
}

export function useChatSession(scopeKind: ChatScopeKind, scopeId: string): UseChatSession {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [bubbles, setBubbles] = useState<ChatBubble[]>([]);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const ensureSession = useCallback(async (): Promise<string | null> => {
    if (sessionId) return sessionId;
    const res = await fetch("/api/chat-sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope_kind: scopeKind, scope_id: scopeId }),
    });
    if (res.status === 503) { setUnavailable(true); return null; }
    if (!res.ok) { setError("Could not start chat"); return null; }
    const data = await res.json();
    setSessionId(data.session_id);
    return data.session_id as string;
  }, [sessionId, scopeKind, scopeId]);

  const send = useCallback(async (text: string) => {
    setError(null);
    const id = await ensureSession();
    if (!id) return;

    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;

    setBubbles((b) => [...b, { role: "user", text }]);
    setWorking(true);

    let res: Response;
    try {
      res = await fetch(`/api/chat-sessions/${id}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: text }),
        signal: ac.signal,
      });
    } catch (e) {
      setWorking(false);
      if (!isAbort(e)) setError("Chat stream failed");
      return;
    }
    if (!res.ok || !res.body) { setWorking(false); setError("Chat stream failed"); return; }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    let startedAssistant = false;

    function handleFrame(frame: string) {
      const lines = frame.split("\n");
      const evType = lines.find((l) => l.startsWith("event:"))?.slice(6).trim();
      const dataLine = lines.find((l) => l.startsWith("data:"))?.slice(5).trim();
      if (!dataLine) return;
      if (evType === "error") {
        try { const d = JSON.parse(dataLine); setError((d && (d.detail || d.type)) || "Chat error"); }
        catch { setError(dataLine || "Chat error"); }
        return;
      }
      if (evType === "done") return;
      try {
        const ev = JSON.parse(dataLine);
        const piece = extractAssistantText(ev);
        if (!piece) return;
        if (!startedAssistant) {
          setBubbles((b) => [...b, { role: "assistant", text: piece }]);
          startedAssistant = true;
        } else {
          setBubbles((b) => {
            const next = [...b];
            const last = next[next.length - 1];
            if (last && last.role === "assistant") next[next.length - 1] = { ...last, text: last.text + piece };
            return next;
          });
        }
      } catch { /* ignore non-JSON keepalive */ }
    }

    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n\n")) !== -1) {
          const frame = buf.slice(0, nl);
          buf = buf.slice(nl + 2);
          handleFrame(frame);
        }
      }
    } catch (e) {
      if (!isAbort(e)) setError("Chat stream failed");
    } finally {
      setWorking(false);
    }
  }, [ensureSession]);

  useEffect(() => () => abortRef.current?.abort(), []);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    setSessionId(null); setBubbles([]); setError(null); setWorking(false);
  }, []);

  const adopt = useCallback((id: string) => {
    abortRef.current?.abort();
    setSessionId(id); setBubbles([]); setError(null); setWorking(false);
  }, []);

  return { sessionId, bubbles, working, error, unavailable, send, reset, adopt };
}

function isAbort(e: unknown): boolean {
  return e instanceof DOMException && e.name === "AbortError";
}

// stream-json assistant text lives in event.message.content[].text for
// type==="assistant"; tolerate the simpler {type:"assistant", text} shape too.
function extractAssistantText(ev: any): string {
  if (ev?.type !== "assistant") return "";
  if (typeof ev.text === "string") return ev.text;
  const content = ev?.message?.content;
  if (Array.isArray(content)) {
    return content.filter((c: any) => c?.type === "text").map((c: any) => c.text).join("");
  }
  return "";
}
