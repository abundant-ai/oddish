"use client";

import { useState, useEffect } from "react";
import useSWR from "swr";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Skeleton } from "@/components/ui/skeleton";
import { Package } from "lucide-react";
import {
  FileRenderer,
  isBinaryRendererFile,
} from "@/components/renderers/file-renderer";
import { fetcher } from "@/lib/api";

const MAX_ARTIFACTS = 10;

interface ArtifactFile {
  path: string;
  key?: string;
  size?: number;
  url?: string;
}

// Harbor writes artifacts inside the per-trial subdirectory of the job dir,
// so the real S3 layout served by /trials/{id}/files is:
//   <trial_name>/artifacts/...                  (single-step)
//   <trial_name>/steps/<step_name>/artifacts/...  (multi-step)
// Treat any file with an `artifacts` segment anywhere in its path as an
// artifact, not just paths that literally begin with "artifacts/".
function isArtifactPath(path: string): boolean {
  return path.split("/").includes("artifacts");
}

// Drop everything up to and including the last `artifacts/` segment so the
// tab label keeps any nested structure (e.g. `screenshots/foo.png`) without
// leaking the Harbor trial-name wrapper dir.
function artifactRelativePath(path: string): string {
  const segments = path.split("/");
  const lastIdx = segments.lastIndexOf("artifacts");
  if (lastIdx === -1) return path;
  return segments.slice(lastIdx + 1).join("/");
}

// Surface the step name for multi-step trials so artifacts collected per
// step don't all collapse to the same basename in the tab list.
function artifactStepName(path: string): string | null {
  const match = path.match(/(?:^|\/)steps\/([^/]+)\/artifacts\//);
  return match ? match[1] : null;
}

function ArtifactContent({
  proxyUrl,
  presignedUrl,
  fileName,
  fileSize,
}: {
  proxyUrl: string;
  presignedUrl?: string;
  fileName: string;
  fileSize?: number;
}) {
  const isBinary = isBinaryRendererFile(fileName);
  // Binary renderers fetch the URL themselves (image src, pdf, etc.) — presigned
  // works directly from the browser and skips the proxy.
  const renderUrl = presignedUrl || proxyUrl;

  const [content, setContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(!isBinary);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (isBinary) {
      setContent(null);
      setLoading(false);
      setError(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);

    async function fetchText() {
      try {
        // Prefer presigned S3 URL (fast, direct). Fall back to backend proxy if
        // the presigned URL is unavailable or fails (expired, CORS, etc.).
        if (presignedUrl) {
          try {
            const res = await fetch(presignedUrl);
            if (res.ok) {
              const text = await res.text();
              if (!cancelled) setContent(text);
              return;
            }
          } catch {
            // fall through to proxy
          }
        }
        const res = await fetch(proxyUrl);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const text = await res.text();
        if (!cancelled) setContent(text);
      } catch (err) {
        if (!cancelled) {
          setError(
            err instanceof Error ? err.message : "Failed to load artifact",
          );
          setContent("");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    void fetchText();
    return () => {
      cancelled = true;
    };
  }, [proxyUrl, presignedUrl, isBinary]);

  if (loading) {
    return (
      <div className="space-y-2 p-4">
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-3/4" />
        <Skeleton className="h-4 w-5/6" />
      </div>
    );
  }

  if (error && !isBinary) {
    return (
      <div className="text-destructive p-4 text-sm">
        Failed to load {fileName}: {error}
      </div>
    );
  }

  return (
    <FileRenderer
      fileName={fileName}
      url={renderUrl}
      content={content}
      fileSize={fileSize}
    />
  );
}

interface ArtifactsViewerProps {
  filesUrl: string;
}

export function ArtifactsViewer({ filesUrl }: ArtifactsViewerProps) {
  const { data, isLoading, error } = useSWR<{
    files: ArtifactFile[];
  }>(`${filesUrl}?recursive=1`, fetcher, {
    revalidateOnFocus: false,
  });

  if (isLoading) {
    return (
      <div className="space-y-2 p-4">
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-3/4" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="text-muted-foreground p-6 text-center text-sm">
        Failed to load artifacts
      </div>
    );
  }

  const artifactFiles = (data?.files ?? []).filter((f) =>
    isArtifactPath(f.path),
  );

  if (artifactFiles.length === 0) {
    return (
      <div className="p-6 text-center">
        <Package className="text-muted-foreground/50 mx-auto mb-2 h-8 w-8" />
        <p className="text-muted-foreground text-sm">No artifacts</p>
        <p className="text-muted-foreground/70 mt-1 text-xs">
          No artifacts were collected from the sandbox
        </p>
      </div>
    );
  }

  const truncated = artifactFiles.length > MAX_ARTIFACTS;
  const displayFiles = artifactFiles.slice(0, MAX_ARTIFACTS);

  const tabs = displayFiles.map((file) => {
    const relativePath = artifactRelativePath(file.path);
    const fileName = relativePath.split("/").pop() ?? relativePath;
    const stepName = artifactStepName(file.path);
    const label = stepName ? `${stepName} / ${relativePath}` : relativePath;
    // Encode the path segment-by-segment so `/` separators in the path are
    // preserved (encodeURIComponent would turn them into %2F, which the
    // backend file route doesn't match).
    const encodedPath = file.path.split("/").map(encodeURIComponent).join("/");
    return {
      id: file.path,
      label,
      fileName,
      fileSize: file.size,
      proxyUrl: `${filesUrl}/${encodedPath}`,
      presignedUrl: file.url,
    };
  });

  return (
    <div className="p-3">
      <Tabs defaultValue={tabs[0].id}>
        <TabsList className="bg-muted/50 h-8 flex-wrap">
          {tabs.map((tab) => (
            <TabsTrigger
              key={tab.id}
              value={tab.id}
              className="px-3 py-1 text-xs"
            >
              {tab.label}
            </TabsTrigger>
          ))}
        </TabsList>
        {tabs.map((tab) => (
          <TabsContent key={tab.id} value={tab.id} className="mt-2">
            <ArtifactContent
              proxyUrl={tab.proxyUrl}
              presignedUrl={tab.presignedUrl}
              fileName={tab.fileName}
              fileSize={tab.fileSize}
            />
          </TabsContent>
        ))}
      </Tabs>
      {truncated && (
        <p className="text-muted-foreground mt-2 text-xs">
          Showing first {MAX_ARTIFACTS} of {artifactFiles.length} artifacts.
        </p>
      )}
    </div>
  );
}
