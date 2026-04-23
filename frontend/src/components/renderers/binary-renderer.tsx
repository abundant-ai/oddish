"use client";

import { Download, FileQuestion } from "lucide-react";
import { Button } from "@/components/ui/button";

interface BinaryRendererProps {
  url: string;
  fileName: string;
  fileSize?: number;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function BinaryRenderer({
  url,
  fileName,
  fileSize,
}: BinaryRendererProps) {
  const ext = fileName.split(".").pop()?.toUpperCase() ?? "BINARY";

  return (
    <div className="flex flex-col items-center justify-center gap-4 p-12 text-center">
      <div className="rounded-full bg-muted p-4">
        <FileQuestion className="h-8 w-8 text-muted-foreground" />
      </div>
      <div>
        <p className="text-sm font-medium text-foreground">{fileName}</p>
        <p className="mt-1 text-xs text-muted-foreground">
          {ext} file{fileSize ? ` · ${formatSize(fileSize)}` : ""}
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          Binary file — no preview available
        </p>
      </div>
      <Button variant="outline" size="sm" asChild>
        <a
          href={url}
          download={fileName}
          target="_blank"
          rel="noopener noreferrer"
        >
          <Download className="mr-2 h-4 w-4" />
          Download
        </a>
      </Button>
    </div>
  );
}
