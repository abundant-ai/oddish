"use client";

import { useEffect, useState } from "react";
import dynamic from "next/dynamic";
import { Loader2 } from "lucide-react";
import { getLanguageFromFilename } from "@/components/code-block";
import { ImageRenderer } from "./image-renderer";
import { VideoRenderer } from "./video-renderer";
import { AudioRenderer } from "./audio-renderer";
import { PdfRenderer } from "./pdf-renderer";
import { BinaryRenderer } from "./binary-renderer";
import { JsonRenderer } from "./json-renderer";
import { LogRenderer } from "./log-renderer";
import { CsvRenderer } from "./csv-renderer";
import { CodeRenderer } from "./code-renderer";
import { TextRenderer } from "./text-renderer";
import { ConfigJsonRenderer } from "./config-json-renderer";

// Heavy renderers are code-split so they don't inflate the main bundle.
const MarkdownRenderer = dynamic(
  () => import("./markdown-renderer").then((m) => m.MarkdownRenderer),
  { ssr: false, loading: () => <LoadingStub label="Rendering markdown..." /> },
);

const NotebookRenderer = dynamic(
  () => import("./notebook-renderer").then((m) => m.NotebookRenderer),
  { ssr: false, loading: () => <LoadingStub label="Rendering notebook..." /> },
);

const XlsxRenderer = dynamic(
  () => import("./xlsx-renderer").then((m) => m.XlsxRenderer),
  { ssr: false, loading: () => <LoadingStub label="Loading spreadsheet..." /> },
);

const DocxRenderer = dynamic(
  () => import("./docx-renderer").then((m) => m.DocxRenderer),
  { ssr: false, loading: () => <LoadingStub label="Converting document..." /> },
);

function LoadingStub({ label }: { label: string }) {
  return (
    <div className="flex h-full items-center justify-center gap-2 p-8 text-muted-foreground">
      <Loader2 className="h-5 w-5 animate-spin" />
      <span>{label}</span>
    </div>
  );
}

export type FileRendererKind =
  | "image"
  | "video"
  | "audio"
  | "pdf"
  | "xlsx"
  | "docx"
  | "markdown"
  | "notebook"
  | "json"
  | "config-json"
  | "csv"
  | "log"
  | "code"
  | "text"
  | "binary";

const IMAGE_EXTS = new Set([
  "png",
  "jpg",
  "jpeg",
  "gif",
  "webp",
  "svg",
  "bmp",
  "ico",
]);
const VIDEO_EXTS = new Set(["mp4", "webm", "ogg", "mov", "avi", "mkv"]);
const AUDIO_EXTS = new Set(["mp3", "wav", "ogg", "flac", "aac", "m4a", "wma"]);
const BINARY_EXTS = new Set([
  "zip",
  "tar",
  "gz",
  "bz2",
  "7z",
  "rar",
  "exe",
  "dll",
  "so",
  "dylib",
  "bin",
  "wasm",
  "pyc",
  "class",
  "o",
  "a",
]);

/** Types that need ArrayBuffer or URL — we should not fetch them as text. */
const BINARY_RENDERER_EXTS = new Set<string>([
  ...IMAGE_EXTS,
  ...VIDEO_EXTS,
  ...AUDIO_EXTS,
  "pdf",
  "xlsx",
  "xls",
  "docx",
  ...BINARY_EXTS,
]);

export function getFileRendererKind(fileName: string): FileRendererKind {
  const lower = fileName.toLowerCase();
  const ext = lower.split(".").pop() ?? "";

  if (IMAGE_EXTS.has(ext)) return "image";
  if (VIDEO_EXTS.has(ext)) return "video";
  if (AUDIO_EXTS.has(ext)) return "audio";
  if (ext === "pdf") return "pdf";
  if (ext === "xlsx" || ext === "xls") return "xlsx";
  if (ext === "docx") return "docx";
  if (ext === "md" || ext === "markdown") return "markdown";
  if (ext === "ipynb") return "notebook";

  if (lower.endsWith("/config.json") || lower === "config.json") {
    return "config-json";
  }
  if (ext === "json") return "json";

  if (ext === "csv" || ext === "tsv") return "csv";
  if (ext === "log") return "log";
  if (BINARY_EXTS.has(ext)) return "binary";

  if (ext === "txt" || ext === "") return "text";

  return "code";
}

export function isBinaryRendererFile(fileName: string): boolean {
  const ext = fileName.toLowerCase().split(".").pop() ?? "";
  return BINARY_RENDERER_EXTS.has(ext);
}

export interface FileRendererProps {
  fileName: string;
  /** URL for media/binary fetches (images, video, audio, pdf, xlsx, docx). */
  url?: string | null;
  /** Text content already fetched by the caller (used for text-based types). */
  content?: string | null;
  /** File size in bytes, used for the binary fallback display. */
  fileSize?: number;
  /** Force a specific renderer regardless of extension. */
  kind?: FileRendererKind;
}

/**
 * Dispatches to the appropriate renderer based on file extension. For binary
 * types (xlsx, docx) the component fetches the URL internally as an
 * ArrayBuffer; for media types the URL is passed directly to the renderer.
 */
export function FileRenderer({
  fileName,
  url,
  content,
  fileSize,
  kind,
}: FileRendererProps) {
  const resolvedKind = kind ?? getFileRendererKind(fileName);

  switch (resolvedKind) {
    case "image":
      if (!url) return <MissingUrl fileName={fileName} />;
      return <ImageRenderer url={url} fileName={fileName} />;
    case "video":
      if (!url) return <MissingUrl fileName={fileName} />;
      return <VideoRenderer url={url} fileName={fileName} />;
    case "audio":
      if (!url) return <MissingUrl fileName={fileName} />;
      return <AudioRenderer url={url} fileName={fileName} />;
    case "pdf":
      if (!url) return <MissingUrl fileName={fileName} />;
      return <PdfRenderer url={url} fileName={fileName} />;
    case "xlsx":
      if (!url) return <MissingUrl fileName={fileName} />;
      return <ArrayBufferWrapper url={url} fileName={fileName} kind="xlsx" />;
    case "docx":
      if (!url) return <MissingUrl fileName={fileName} />;
      return <ArrayBufferWrapper url={url} fileName={fileName} kind="docx" />;
    case "binary":
      return (
        <BinaryRenderer
          url={url ?? "#"}
          fileName={fileName}
          fileSize={fileSize}
        />
      );
    case "markdown":
      return <MarkdownRenderer content={content ?? ""} />;
    case "notebook":
      return <NotebookRenderer content={content ?? ""} />;
    case "json":
      return <JsonRenderer content={content ?? ""} />;
    case "config-json":
      return <ConfigJsonRenderer content={content ?? ""} />;
    case "csv": {
      const delimiter = fileName.toLowerCase().endsWith(".tsv") ? "\t" : ",";
      return <CsvRenderer content={content ?? ""} delimiter={delimiter} />;
    }
    case "log":
      return <LogRenderer content={content ?? ""} />;
    case "text":
      return <TextRenderer content={content ?? ""} />;
    case "code":
    default:
      return (
        <CodeRenderer
          content={content ?? ""}
          language={getLanguageFromFilename(fileName)}
        />
      );
  }
}

function MissingUrl({ fileName }: { fileName: string }) {
  return (
    <div className="flex h-full items-center justify-center p-8 text-sm text-muted-foreground">
      Cannot preview {fileName}: no URL available
    </div>
  );
}

function ArrayBufferWrapper({
  url,
  fileName,
  kind,
}: {
  url: string;
  fileName: string;
  kind: "xlsx" | "docx";
}) {
  const [data, setData] = useState<ArrayBuffer | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    setError(null);
    fetch(url)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.arrayBuffer();
      })
      .then((buf) => {
        if (!cancelled) setData(buf);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const message =
          err instanceof Error ? err.message : "Failed to fetch file";
        setError(message);
      });
    return () => {
      cancelled = true;
    };
  }, [url]);

  if (error) {
    return (
      <div className="p-4 text-sm text-destructive">
        Failed to load {fileName}: {error}
      </div>
    );
  }

  if (data === null) {
    return (
      <LoadingStub
        label={
          kind === "xlsx" ? "Loading spreadsheet..." : "Loading document..."
        }
      />
    );
  }

  if (kind === "xlsx") {
    return <XlsxRenderer data={data} fileName={fileName} />;
  }
  return <DocxRenderer data={data} fileName={fileName} />;
}
