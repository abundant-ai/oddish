"use client";

interface ImageRendererProps {
  url: string;
  fileName: string;
}

export function ImageRenderer({ url, fileName }: ImageRendererProps) {
  return (
    <div className="flex items-center justify-center bg-muted/50 p-8">
      <img
        src={url}
        alt={fileName}
        className="max-h-[600px] max-w-full rounded-lg border border-border object-contain"
      />
    </div>
  );
}
