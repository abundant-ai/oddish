"use client";

import { useEffect, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import type { ChatScopeKind, ChatSessionSummary } from "@/lib/cc-chat-types";

export function ChatHistoryModal({
  open,
  onOpenChange,
  scopeKind,
  scopeId,
  onPick,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  scopeKind: ChatScopeKind;
  scopeId: string;
  onPick: (id: string) => void;
}) {
  const [q, setQ] = useState("");
  const [items, setItems] = useState<ChatSessionSummary[]>([]);
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const limit = 10;

  useEffect(() => {
    if (!open) return;
    const params = new URLSearchParams({
      scope_kind: scopeKind,
      scope_id: scopeId,
      limit: String(limit),
      offset: String(offset),
    });
    if (q) params.set("q", q);
    fetch(`/api/chat-sessions?${params.toString()}`)
      .then((r) => r.json())
      .then((d) => {
        setItems(d.sessions ?? []);
        setTotal(d.total ?? 0);
      })
      .catch(() => {
        setItems([]);
        setTotal(0);
      });
  }, [open, scopeKind, scopeId, q, offset]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Chat history</DialogTitle>
        </DialogHeader>
        <input
          className="bg-background mb-2 w-full rounded-md border px-2 py-1.5 text-sm"
          placeholder="Search past chats…"
          value={q}
          onChange={(e) => {
            setOffset(0);
            setQ(e.target.value);
          }}
        />
        <div className="flex max-h-[50vh] flex-col gap-1 overflow-y-auto">
          {items.length === 0 ? (
            <div className="text-muted-foreground p-3 text-sm">
              No chats yet.
            </div>
          ) : (
            items.map((s) => (
              <button
                key={s.id}
                onClick={() => {
                  onPick(s.id);
                  onOpenChange(false);
                }}
                className="hover:bg-accent flex flex-col items-start rounded-md px-2 py-1.5 text-left"
              >
                <span className="text-sm">{s.title ?? "Untitled chat"}</span>
                <span className="text-muted-foreground text-xs">
                  {new Date(s.last_activity).toLocaleString()} · {s.status} ·{" "}
                  {s.turn_count} msgs
                </span>
              </button>
            ))
          )}
        </div>
        {offset + limit < total ? (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setOffset(offset + limit)}
          >
            Load more
          </Button>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
