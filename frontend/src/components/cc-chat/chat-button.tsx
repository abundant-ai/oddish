"use client";

import { useState } from "react";
import { MessageSquare } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { ChatScopeKind } from "@/lib/cc-chat-types";
import { ChatDrawer } from "./chat-drawer";

export function ChatButton({
  scopeKind,
  scopeId,
}: {
  scopeKind: ChatScopeKind;
  scopeId: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button
        size="sm"
        className="gap-1.5 bg-blue-600 text-white hover:bg-blue-700"
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen(true)}
      >
        <MessageSquare className="h-3.5 w-3.5" /> Chat
      </Button>
      <ChatDrawer
        open={open}
        onOpenChange={setOpen}
        scopeKind={scopeKind}
        scopeId={scopeId}
      />
    </>
  );
}
