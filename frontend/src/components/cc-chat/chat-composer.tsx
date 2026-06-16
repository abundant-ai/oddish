"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";

export function ChatComposer({
  disabled,
  onSend,
}: {
  disabled: boolean;
  onSend: (text: string) => void;
}) {
  const [value, setValue] = useState("");
  const submit = () => {
    const t = value.trim();
    if (!t || disabled) return;
    onSend(t);
    setValue("");
  };
  return (
    <div className="border-border flex items-end gap-2 border-t p-2">
      <textarea
        className="bg-background min-h-[38px] flex-1 resize-none rounded-md border px-2 py-1.5 text-sm"
        rows={1}
        value={value}
        placeholder="Ask about this scope…"
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
        }}
        disabled={disabled}
      />
      <Button size="sm" onClick={submit} disabled={disabled || !value.trim()}>
        Send
      </Button>
    </div>
  );
}
