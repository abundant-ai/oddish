"use client";

interface RawRendererProps {
  content: string;
}

export function RawRenderer({ content }: RawRendererProps) {
  return (
    <pre className="overflow-auto whitespace-pre p-3 font-mono text-xs text-foreground">
      {content}
    </pre>
  );
}
