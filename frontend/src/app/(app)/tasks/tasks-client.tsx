"use client";

import { useEffect, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { Clock, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ChatButton } from "@/components/cc-chat/chat-button";
import { ImportDialog } from "@/components/import-dialog";
import { cn } from "@/lib/utils";

const AUTO_REFRESH_KEY = "oddish.tasks.autoRefresh";
const REFRESH_MS = 60000;

// Header actions for the tasks page: chat, a manual refresh of the
// server-rendered results, an opt-in auto-refresh toggle (off by default,
// persisted), and import. Search and tag filtering live in the sidebar.
export function TasksToolbar({ orgId = null }: { orgId?: string | null }) {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();
  const [autoRefresh, setAutoRefresh] = useState(false);

  // Restore the saved preference client-side (avoids a hydration mismatch).
  useEffect(() => {
    setAutoRefresh(window.localStorage.getItem(AUTO_REFRESH_KEY) === "1");
  }, []);

  const toggleAuto = () => {
    setAutoRefresh((prev) => {
      const next = !prev;
      window.localStorage.setItem(AUTO_REFRESH_KEY, next ? "1" : "0");
      return next;
    });
  };

  // Silent background refresh only while auto-refresh is on.
  useEffect(() => {
    if (!autoRefresh) return;
    const id = window.setInterval(() => router.refresh(), REFRESH_MS);
    return () => window.clearInterval(id);
  }, [autoRefresh, router]);

  const manualRefresh = () => startTransition(() => router.refresh());

  return (
    <div className="flex items-center gap-2">
      {orgId ? <ChatButton scopeKind="global" scopeId={orgId} /> : null}
      <Button
        type="button"
        variant="outline"
        size="icon"
        className="h-8 w-8 border-[#6f88b4]/20"
        onClick={manualRefresh}
        disabled={isPending}
        aria-label="Refresh tasks"
        title="Refresh tasks"
      >
        <RefreshCw className={cn("h-4 w-4", isPending && "animate-spin")} />
      </Button>
      <Button
        type="button"
        variant={autoRefresh ? "default" : "outline"}
        size="sm"
        className={cn(
          "h-8 gap-1.5 text-xs",
          !autoRefresh && "border-[#6f88b4]/20"
        )}
        onClick={toggleAuto}
        aria-pressed={autoRefresh}
        title={
          autoRefresh
            ? "Auto-refresh on (every 60s) — click to turn off"
            : "Auto-refresh off — click to refresh every 60s"
        }
      >
        <Clock className="h-3.5 w-3.5" />
        Auto
      </Button>
      <ImportDialog onImported={() => router.refresh()} />
    </div>
  );
}
