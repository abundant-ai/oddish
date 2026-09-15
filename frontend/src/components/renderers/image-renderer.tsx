"use client";

interface ImageRendererProps {
  url: string;
  fileName: string;
  onError?: () => void;
}

export function ImageRenderer({ url, fileName, onError }: ImageRendererProps) {
  return (
    <div className="bg-muted/50 flex items-center justify-center p-8">
      <img
        src={url}
        alt={fileName}
        onError={onError}
        className="border-border max-h-[600px] max-w-full rounded-lg border object-contain"
      />
    </div>
  );
}
