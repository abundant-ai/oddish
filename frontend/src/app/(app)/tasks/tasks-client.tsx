"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { ChatButton } from "@/components/cc-chat/chat-button";
import { ImportDialog } from "@/components/import-dialog";

// Header actions for the tasks page. Search and tag filtering now live in the
// sidebar; this keeps the page-level actions (chat, import) plus the old 60s
// auto-refresh of the server-rendered results.
export function TasksToolbar({ orgId = null }: { orgId?: string | null }) {
  const router = useRouter();

  useEffect(() => {
    const id = window.setInterval(() => router.refresh(), 60000);
    return () => window.clearInterval(id);
  }, [router]);

  return (
    <div className="flex items-center gap-2">
      {orgId ? <ChatButton scopeKind="global" scopeId={orgId} /> : null}
      <ImportDialog onImported={() => router.refresh()} />
    </div>
  );
}
