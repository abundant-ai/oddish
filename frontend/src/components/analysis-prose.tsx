"use client";

import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";

/** Compact markdown for QA report prose. Renders bullets, inline code, and
 *  bold at card size. QA prompts limit reports to those forms, so headings
 *  and code fences are not styled here. */
export function AnalysisProse({
  text,
  className,
}: {
  text: string;
  className?: string;
}) {
  return (
    <div className={cn("text-xs leading-relaxed", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkBreaks]}
        components={{
          p: ({ children }) => <p className="mb-1.5 last:mb-0">{children}</p>,
          ul: ({ children }) => (
            <ul className="mb-1.5 ml-4 list-outside list-disc space-y-1 last:mb-0">
              {children}
            </ul>
          ),
          ol: ({ children }) => (
            <ol className="mb-1.5 ml-4 list-outside list-decimal space-y-1 last:mb-0">
              {children}
            </ol>
          ),
          code: ({ children }) => (
            <code className="bg-muted text-foreground/90 rounded px-1 py-0.5 font-mono text-[0.9em]">
              {children}
            </code>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
