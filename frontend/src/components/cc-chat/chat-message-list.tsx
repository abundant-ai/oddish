"use client";

import { useEffect, useRef } from "react";
import type { ChatBubble } from "@/lib/cc-chat-types";
import { cn } from "@/lib/utils";

export function ChatMessageList({ bubbles, working }: { bubbles: ChatBubble[]; working: boolean }) {
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [bubbles, working]);

  return (
    <div className="flex flex-1 flex-col gap-2 overflow-y-auto p-3 text-sm">
      {bubbles.map((b, i) => (
        <div
          key={i}
          className={cn(
            "max-w-[80%] rounded-lg px-3 py-2 whitespace-pre-wrap",
            b.role === "user" ? "self-end bg-muted" : "self-start bg-accent/40",
          )}
        >
          {b.text}
        </div>
      ))}
      {working ? (
        <div className="text-muted-foreground self-start px-1 text-xs italic">● working…</div>
      ) : null}
      <div ref={endRef} />
    </div>
  );
}
