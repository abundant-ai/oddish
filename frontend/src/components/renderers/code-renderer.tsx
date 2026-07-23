"use client";

import { CodeBlock } from "@/components/code-block";

interface CodeRendererProps {
  content: string;
  language: string;
  highlightLines?: [number, number] | null;
}

export function CodeRenderer({
  content,
  language,
  highlightLines,
}: CodeRendererProps) {
  return (
    <div className="h-full overflow-auto">
      <CodeBlock
        code={content}
        language={language}
        maxHeight="none"
        truncateAt={0}
        highlightLines={highlightLines}
      />
    </div>
  );
}
