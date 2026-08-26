"use client";

import { useEffect, useRef, useState } from "react";
import { Check, Copy } from "lucide-react";
import { Button } from "@/components/ui/button";

export function ExperimentQaCopyLinkButton({ href }: { href: string }) {
  const [status, setStatus] = useState<"idle" | "copied" | "error">("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    []
  );

  async function copy() {
    if (timer.current) clearTimeout(timer.current);
    try {
      if (!navigator.clipboard) throw new Error("Clipboard is not available");
      await navigator.clipboard.writeText(`${window.location.origin}${href}`);
      setStatus("copied");
    } catch {
      setStatus("error");
    }
    timer.current = setTimeout(() => setStatus("idle"), 1600);
  }

  const copied = status === "copied";
  const label = copied
    ? "Copied"
    : status === "error"
      ? "Copy failed"
      : "Copy link";
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className="h-8"
      onClick={() => void copy()}
      aria-label={
        copied
          ? "Public QA link copied"
          : status === "error"
            ? "Public QA link could not be copied"
            : "Copy public QA link"
      }
      aria-live="polite"
    >
      {copied ? (
        <Check className="size-3.5" aria-hidden="true" />
      ) : (
        <Copy className="size-3.5" aria-hidden="true" />
      )}
      {label}
    </Button>
  );
}
