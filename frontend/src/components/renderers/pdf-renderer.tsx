"use client";

interface PdfRendererProps {
  url: string;
  fileName: string;
}

export function PdfRenderer({ url, fileName }: PdfRendererProps) {
  return (
    <div className="flex h-full min-h-[600px] flex-col">
      <iframe
        src={url}
        title={fileName}
        className="min-h-[600px] w-full flex-1 rounded-lg border-0"
        sandbox="allow-same-origin"
      />
    </div>
  );
}
