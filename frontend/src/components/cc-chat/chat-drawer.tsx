"use client";

import { useState } from "react";
import { ResizableDrawer } from "@/components/ui/resizable-drawer";
import { Button } from "@/components/ui/button";
import { History, Plus } from "lucide-react";
import type { ChatScopeKind } from "@/lib/cc-chat-types";
import { useChatSession } from "./use-chat-session";
import { ChatMessageList } from "./chat-message-list";
import { ChatComposer } from "./chat-composer";
import { ChatHistoryModal } from "./chat-history-modal";

export function ChatDrawer({
  open,
  onOpenChange,
  scopeKind,
  scopeId,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  scopeKind: ChatScopeKind;
  scopeId: string;
}) {
  const chat = useChatSession(scopeKind, scopeId);
  const [historyOpen, setHistoryOpen] = useState(false);

  return (
    <ResizableDrawer
      open={open}
      onOpenChange={onOpenChange}
      defaultWidth={520}
      minWidth={380}
      maxWidth={900}
    >
      <div className="flex h-full flex-col">
        <div className="border-border flex items-center justify-between border-b px-3 py-2">
          <span className="text-sm font-semibold">Chat</span>
          <div className="flex items-center gap-1">
            <Button
              size="sm"
              variant="ghost"
              className="gap-1"
              onClick={() => setHistoryOpen(true)}
            >
              <History className="h-3.5 w-3.5" /> History
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="gap-1"
              onClick={chat.reset}
            >
              <Plus className="h-3.5 w-3.5" /> New chat
            </Button>
          </div>
        </div>
        {chat.unavailable ? (
          <div className="text-muted-foreground p-4 text-sm">
            Chat is unavailable in this environment.
          </div>
        ) : (
          <>
            <ChatMessageList bubbles={chat.bubbles} working={chat.working} />
            {chat.error ? (
              <div className="px-3 py-1 text-xs text-red-600">{chat.error}</div>
            ) : null}
            <ChatComposer disabled={chat.unavailable || chat.working} onSend={chat.send} />
          </>
        )}
      </div>
      <ChatHistoryModal
        open={historyOpen}
        onOpenChange={setHistoryOpen}
        scopeKind={scopeKind}
        scopeId={scopeId}
        onPick={(id) => chat.resume(id)}
      />
    </ResizableDrawer>
  );
}
