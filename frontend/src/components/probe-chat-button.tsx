"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { CCChatModal } from "@/components/cc-chat-modal";

export function ProbeChatButton({ taskId }: { taskId: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => setOpen(true)}
      >
        Chat with probe runs
      </Button>
      <CCChatModal
        scope={{ kind: "task", taskId }}
        open={open}
        onOpenChange={setOpen}
      />
    </>
  );
}
