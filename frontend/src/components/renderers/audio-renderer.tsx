"use client";

import { Music } from "lucide-react";

interface AudioRendererProps {
  url: string;
  fileName: string;
}

const MIME_MAP: Record<string, string> = {
  mp3: "audio/mpeg",
  wav: "audio/wav",
  ogg: "audio/ogg",
  flac: "audio/flac",
  aac: "audio/aac",
  m4a: "audio/mp4",
  wma: "audio/x-ms-wma",
};

export function AudioRenderer({ url, fileName }: AudioRendererProps) {
  const ext = fileName.split(".").pop()?.toLowerCase() ?? "";
  const mime = MIME_MAP[ext] ?? "audio/mpeg";

  return (
    <div className="flex flex-col items-center justify-center gap-4 p-12">
      <div className="rounded-full bg-muted p-4">
        <Music className="h-8 w-8 text-muted-foreground" />
      </div>
      <p className="text-sm font-medium text-foreground">{fileName}</p>
      <audio controls preload="metadata" className="w-full max-w-md">
        <source src={url} type={mime} />
        Your browser does not support this audio format.
      </audio>
    </div>
  );
}
