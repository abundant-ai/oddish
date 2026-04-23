"use client";

interface VideoRendererProps {
  url: string;
  fileName: string;
}

const MIME_MAP: Record<string, string> = {
  mp4: "video/mp4",
  webm: "video/webm",
  ogg: "video/ogg",
  mov: "video/quicktime",
  avi: "video/x-msvideo",
  mkv: "video/x-matroska",
};

export function VideoRenderer({ url, fileName }: VideoRendererProps) {
  const ext = fileName.split(".").pop()?.toLowerCase() ?? "";
  const mime = MIME_MAP[ext] ?? "video/mp4";

  return (
    <div className="flex flex-col items-center justify-center gap-3 p-6">
      <video
        controls
        preload="metadata"
        className="max-h-[500px] max-w-full rounded-lg border border-border"
      >
        <source src={url} type={mime} />
        Your browser does not support this video format.
      </video>
      <p className="text-xs text-muted-foreground">{fileName}</p>
    </div>
  );
}
