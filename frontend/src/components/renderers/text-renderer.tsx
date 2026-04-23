"use client";

interface TextRendererProps {
  content: string;
}

export function TextRenderer({ content }: TextRendererProps) {
  return (
    <div className="overflow-auto whitespace-pre-wrap p-3 font-mono text-xs text-foreground">
      {content}
    </div>
  );
}
