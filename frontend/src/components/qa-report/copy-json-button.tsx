"use client";

import { useEffect, useRef, useState } from "react";
import { Check, Copy } from "lucide-react";
import { cn } from "@/lib/utils";

export function CopyJsonButton({
  value,
  label,
  compact = false,
  className,
}: {
  value: unknown;
  /** describes what gets copied, for the accessible name */
  label: string;
  compact?: boolean;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, []);

  async function copy(event: React.MouseEvent) {
    // The compact variant sits inside a <summary>; a plain click there also
    // toggles the disclosure the button lives in.
    event.preventDefault();
    event.stopPropagation();
    const json = JSON.stringify(value, null, 2);
    try {
      await navigator.clipboard.writeText(json);
    } catch {
      // Clipboard API is unavailable in some embedded/insecure contexts.
      const ta = document.createElement("textarea");
      ta.value = json;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    setCopied(true);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied(false), 1600);
  }

  const Icon = copied ? Check : Copy;

  return (
    <button
      type="button"
      onClick={copy}
      aria-label={
        copied ? `${label} copied to clipboard` : `Copy ${label} as JSON`
      }
      className={cn(
        "focus-visible:ring-ring inline-flex shrink-0 items-center gap-1.5 rounded-md border font-mono text-[10px] transition-colors focus-visible:ring-2 focus-visible:outline-none",
        compact ? "px-1.5 py-1" : "px-2 py-1",
        copied
          ? "border-emerald-500/60 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
          : "border-border text-muted-foreground hover:bg-secondary hover:text-foreground",
        className,
      )}
    >
      <Icon aria-hidden="true" className="size-3" strokeWidth={2} />
      {compact ? null : <span>{copied ? "COPIED" : "COPY JSON"}</span>}
    </button>
  );
}
