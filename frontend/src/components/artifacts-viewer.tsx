"use client";

import { useState, useEffect } from "react";
import useSWR from "swr";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Skeleton } from "@/components/ui/skeleton";
import { Package, ImageOff } from "lucide-react";
import { CodeBlock, getLanguageFromFilename } from "@/components/code-block";
import { fetcher } from "@/lib/api";

const IMAGE_EXTENSIONS = new Set([
  "png",
  "jpg",
  "jpeg",
  "gif",
  "webp",
  "svg",
]);
const MAX_ARTIFACTS = 10;

function isImageFile(filename: string): boolean {
  const ext = filename.split(".").pop()?.toLowerCase() ?? "";
  return IMAGE_EXTENSIONS.has(ext);
}

interface ArtifactFile {
  path: string;
  key?: string;
  size?: number;
  url?: string;
}

function ArtifactImageContent({
  filesUrl,
  filePath,
}: {
  filesUrl: string;
  filePath: string;
}) {
  const [error, setError] = useState(false);
  const encodedPath = encodeURIComponent(filePath);
  const src = `${filesUrl}/${encodedPath}`;

  if (error) {
    return (
      <div className="p-4 flex items-center gap-2 text-sm text-muted-foreground">
        <ImageOff className="h-4 w-4" />
        Failed to load image: {filePath}
      </div>
    );
  }

  return (
    <div className="p-4">
      <img
        src={src}
        alt={filePath}
        className="max-w-full h-auto rounded border border-border"
        style={{ maxHeight: "600px" }}
        loading="lazy"
        onError={() => setError(true)}
      />
    </div>
  );
}

function ArtifactFileContent({
  filesUrl,
  filePath,
  language,
}: {
  filesUrl: string;
  filePath: string;
  language: string;
}) {
  const [content, setContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;

    async function fetchContent() {
      setLoading(true);
      try {
        const encodedPath = encodeURIComponent(filePath);
        const res = await fetch(`${filesUrl}/${encodedPath}`);
        if (res.ok) {
          const text = await res.text();
          if (!cancelled) setContent(text);
        }
      } catch {
        // silently fail
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    fetchContent();
    return () => {
      cancelled = true;
    };
  }, [filesUrl, filePath]);

  if (loading) {
    return (
      <div className="p-4 space-y-2">
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-3/4" />
        <Skeleton className="h-4 w-5/6" />
      </div>
    );
  }

  return (
    <CodeBlock
      code={content ?? ""}
      language={language}
      maxHeight="24rem"
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
      <div className="p-4 space-y-2">
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-3/4" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-6 text-center text-sm text-muted-foreground">
        Failed to load artifacts
      </div>
    );
  }

  const artifactFiles = (data?.files ?? []).filter((f) =>
    f.path.startsWith("artifacts/"),
  );

  if (artifactFiles.length === 0) {
    return (
      <div className="p-6 text-center">
        <Package className="h-8 w-8 text-muted-foreground/50 mx-auto mb-2" />
        <p className="text-sm text-muted-foreground">No artifacts</p>
        <p className="text-xs text-muted-foreground/70 mt-1">
          No artifacts were collected from the sandbox
        </p>
      </div>
    );
  }

  const truncated = artifactFiles.length > MAX_ARTIFACTS;
  const displayFiles = artifactFiles.slice(0, MAX_ARTIFACTS);

  const tabs = displayFiles.map((file) => {
    const relativePath = file.path.replace(/^artifacts\//, "");
    const fileName = relativePath.split("/").pop() ?? relativePath;
    return {
      id: file.path,
      label: fileName,
      fullPath: file.path,
      language: getLanguageFromFilename(fileName),
      isImage: isImageFile(fileName),
    };
  });

  return (
    <div className="p-3">
      <Tabs defaultValue={tabs[0].id}>
        <TabsList className="h-8 bg-muted/50 flex-wrap">
          {tabs.map((tab) => (
            <TabsTrigger
              key={tab.id}
              value={tab.id}
              className="text-xs px-3 py-1"
            >
              {tab.label}
            </TabsTrigger>
          ))}
        </TabsList>
        {tabs.map((tab) => (
          <TabsContent key={tab.id} value={tab.id} className="mt-2">
            {tab.isImage ? (
              <ArtifactImageContent
                filesUrl={filesUrl}
                filePath={tab.fullPath}
              />
            ) : (
              <ArtifactFileContent
                filesUrl={filesUrl}
                filePath={tab.fullPath}
                language={tab.language}
              />
            )}
          </TabsContent>
        ))}
      </Tabs>
      {truncated && (
        <p className="text-xs text-muted-foreground mt-2">
          Showing first {MAX_ARTIFACTS} of {artifactFiles.length} artifacts.
        </p>
      )}
    </div>
  );
}
