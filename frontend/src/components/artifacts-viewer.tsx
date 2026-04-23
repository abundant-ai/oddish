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

function ArtifactContent({
  filesUrl,
  filePath,
  fileName,
  fileSize,
}: {
  filesUrl: string;
  filePath: string;
  fileName: string;
  fileSize?: number;
}) {
  const encodedPath = encodeURIComponent(filePath);
  const url = `${filesUrl}/${encodedPath}`;
  const isBinary = isBinaryRendererFile(fileName);

  const [content, setContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(!isBinary);

  useEffect(() => {
    if (isBinary) {
      setContent(null);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    fetch(url)
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.text();
      })
      .then((text) => {
        if (!cancelled) setContent(text);
      })
      .catch(() => {
        if (!cancelled) setContent("");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [url, isBinary]);

  if (loading) {
    return (
      <div className="space-y-2 p-4">
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-3/4" />
        <Skeleton className="h-4 w-5/6" />
      </div>
    );
  }

  return (
    <FileRenderer
      fileName={fileName}
      url={url}
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
        <Package className="mx-auto mb-2 h-8 w-8 text-muted-foreground/50" />
        <p className="text-sm text-muted-foreground">No artifacts</p>
        <p className="mt-1 text-xs text-muted-foreground/70">
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
      fileName,
      fileSize: file.size,
    };
  });

  return (
    <div className="p-3">
      <Tabs defaultValue={tabs[0].id}>
        <TabsList className="h-8 flex-wrap bg-muted/50">
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
              filesUrl={filesUrl}
              filePath={tab.fullPath}
              fileName={tab.fileName}
              fileSize={tab.fileSize}
            />
          </TabsContent>
        ))}
      </Tabs>
      {truncated && (
        <p className="mt-2 text-xs text-muted-foreground">
          Showing first {MAX_ARTIFACTS} of {artifactFiles.length} artifacts.
        </p>
      )}
    </div>
  );
}
