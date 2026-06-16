"use client";

import { useCallback, useRef, useState } from "react";
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
  const assistantRef = useRef<string>("");

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
    setBubbles((b) => [...b, { role: "user", text }]);
    setWorking(true);
    assistantRef.current = "";

    const res = await fetch(`/api/chat-sessions/${id}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: text }),
    });
    if (!res.ok || !res.body) { setWorking(false); setError("Chat stream failed"); return; }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";

    function handleFrame(frame: string) {
      const lines = frame.split("\n");
      const evType = lines.find((l) => l.startsWith("event:"))?.slice(6).trim();
      const dataLine = lines.find((l) => l.startsWith("data:"))?.slice(5).trim();
      if (!dataLine) return;
      if (evType === "error") { setError("Chat error"); return; }
      if (evType === "done") return;
      try {
        const ev = JSON.parse(dataLine);
        const piece = extractAssistantText(ev);
        if (piece) assistantRef.current += piece;
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
    } finally {
      if (assistantRef.current) {
        const finalText = assistantRef.current;
        setBubbles((b) => [...b, { role: "assistant", text: finalText }]);
      }
      setWorking(false);
    }
  }, [ensureSession]);

  const reset = useCallback(() => {
    setSessionId(null); setBubbles([]); setError(null); setWorking(false);
  }, []);

  const adopt = useCallback((id: string) => {
    setSessionId(id); setBubbles([]); setError(null); setWorking(false);
  }, []);

  return { sessionId, bubbles, working, error, unavailable, send, reset, adopt };
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
