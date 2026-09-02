"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Code, Eye, Loader2, Package } from "lucide-react";
import { FileTreePane } from "@/components/file-tree-pane";
import {
  FileRenderer,
  isBinaryRendererFile,
} from "@/components/renderers/file-renderer";
import { fetcher } from "@/lib/api";
import { firstFilePath } from "@/lib/file-tree-order";
import { formatFileSize } from "@/lib/format";
import { sameFilePath } from "@/lib/file-path";
import { recordClientError } from "@/lib/observability";
import type { LineRange } from "@/lib/line-range";

// Preview truncation threshold; matches TaskFilesPanel.
const TRUNCATE_THRESHOLD = 100 * 1024;

interface ArtifactFile {
  path: string;
  key?: string;
  size?: number;
  url?: string;
}

interface ArtifactsListing {
  files?: ArtifactFile[];
}

interface ArtifactEntry {
  // Relative path inside the synthetic artifact root — the tree row's
  // identity, stripped of the Harbor `<trial_name>/` (and `steps/<step>/`)
  // wrapper dirs so the tree reads like a normal filesystem.
  path: string;
  // Original S3-relative path returned by /trials/{id}/files. Used to build
  // the backend proxy URL for content fetches.
  fullPath: string;
  size?: number;
  url?: string;
}

// Harbor writes artifacts inside the per-trial subdirectory of the job dir,
// so the real S3 layout served by /trials/{id}/files is:
//   <trial_name>/artifacts/...                     (single-step)
//   <trial_name>/steps/<step_name>/artifacts/...   (multi-step)
// Treat any file with an `artifacts` segment anywhere in its path as an
// artifact, not just paths that literally begin with "artifacts/".
function isArtifactPath(path: string): boolean {
  return path.split("/").includes("artifacts");
}

// Strip the Harbor wrapper dirs before `artifacts/` so the tree shows clean
// paths. For multi-step trials, prefix with the step name so per-step
// artifacts get grouped together (e.g. `setup/log.txt`, `main/result.json`).
function relativizeArtifactPath(path: string): string {
  const segments = path.split("/");
  const lastArtifactsIdx = segments.lastIndexOf("artifacts");
  if (lastArtifactsIdx === -1) return path;
  const inside = segments.slice(lastArtifactsIdx + 1).join("/");
  const stepsIdx = segments.indexOf("steps");
  if (
    stepsIdx !== -1 &&
    stepsIdx < lastArtifactsIdx &&
    segments[stepsIdx + 1]
  ) {
    return `${segments[stepsIdx + 1]}/${inside}`;
  }
  return inside;
}

/**
 * One entry per artifact file, keyed by relativized path. Colliding
 * relativized paths (a multi-step and a single-step artifact reducing to the
 * same name) keep the first entry — `path` is the tree row's identity.
 */
function buildArtifactEntries(
  files: ArtifactFile[]
): Map<string, ArtifactEntry> {
  const entries = new Map<string, ArtifactEntry>();
  for (const file of files) {
    const path = relativizeArtifactPath(file.path);
    if (!path || entries.has(path)) continue;
    entries.set(path, {
      path,
      fullPath: file.path,
      size: file.size,
      url: file.url,
    });
  }
  return entries;
}

interface ArtifactsViewerProps {
  filesUrl: string;
  trialId?: string;
  successfulAnalysisTrial?: boolean;
  /**
   * Deep-linked file to select once the listing loads (``?file=`` while
   * ``tab=artifacts``). Accepts the tree path shown in the browser, the
   * original storage path, or a suffix of either (bare file name).
   */
  initialFilePath?: string | null;
  /** Line range to highlight in the selected file (``?lines=``). */
  selectedLines?: LineRange | null;
  onSelectLinesChange?: (range: LineRange | null) => void;
  /**
   * Reports the selected file's tree path (and its original storage path,
   * when known) whenever a file is selected, for URL sync. The storage path
   * lets the parent recognize a deep link that addressed the file by
   * storage path — the two forms differ for multi-step artifacts. Never
   * called with null — transient resets are not reported.
   */
  onSelectedFileChange?: (path: string, fullPath?: string) => void;
}

export function ArtifactsViewer({
  filesUrl,
  trialId,
  successfulAnalysisTrial = false,
  initialFilePath,
  selectedLines,
  onSelectLinesChange,
  onSelectedFileChange,
}: ArtifactsViewerProps) {
  const { data, isLoading, isValidating, error, mutate } =
    useSWR<ArtifactsListing>(`${filesUrl}?recursive=1`, fetcher, {
      revalidateOnFocus: false,
    });
  const errorStatus = (error as { status?: number } | undefined)?.status;
  const reportedIntegrityFailureRef = useRef<{
    trialId: string | undefined;
    filesUrl: string;
  } | null>(null);
  useEffect(() => {
    const reportedFailure = reportedIntegrityFailureRef.current;
    if (
      errorStatus !== 404 ||
      !successfulAnalysisTrial ||
      (reportedFailure !== null &&
        reportedFailure.trialId === trialId &&
        reportedFailure.filesUrl === filesUrl)
    ) {
      return;
    }
    reportedIntegrityFailureRef.current = { trialId, filesUrl };
    recordClientError("artifact_integrity_failure", {
      trial_id: trialId ?? "unknown",
      files_url: filesUrl,
      http_status: 404,
    });
  }, [errorStatus, filesUrl, successfulAnalysisTrial, trialId]);

  const entriesByPath = useMemo(
    () =>
      buildArtifactEntries(
        (data?.files ?? []).filter((f) => isArtifactPath(f.path))
      ),
    [data]
  );

  // Identity must only change with the listing — FileTreePane rebuilds (and
  // re-expands) the tree on any new array.
  const treePaths = useMemo(() => [...entriesByPath.keys()], [entriesByPath]);

  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<"rendered" | "raw">("rendered");

  // A deep-linked path owns the first selection; read through a ref so the
  // load effect doesn't re-run when the parent echoes selections back.
  const initialFilePathRef = useRef(initialFilePath);
  useEffect(() => {
    initialFilePathRef.current = initialFilePath;
  });

  // Re-runs as the listing grows (a live trial keeps producing artifacts);
  // a still-present selection is kept.
  useEffect(() => {
    if (!entriesByPath.size) {
      setSelectedPath(null);
      return;
    }
    setSelectedPath((prev) => {
      if (prev && entriesByPath.has(prev)) return prev;
      const wanted = initialFilePathRef.current;
      if (wanted) {
        // Match against the relativized tree path and the original storage
        // path: multi-step artifacts insert the step segment into the tree
        // path (steps/setup/artifacts/x → setup/x), so a storage path from
        // the files API is not a suffix of it and only fullPath can match.
        const entries = [...entriesByPath.values()];
        const match =
          entriesByPath.get(wanted) ??
          entries.find((f) => f.fullPath === wanted) ??
          entries.find((f) => sameFilePath(f.path, wanted)) ??
          entries.find((f) => sameFilePath(f.fullPath, wanted));
        // An unresolved deep link keeps the selection empty instead of
        // falling through to the first file: reporting that fallback
        // would wipe the ?file= / ?lines= address it couldn't resolve.
        // The effect re-runs as the listing grows, so a late-arriving
        // artifact still resolves.
        return match?.path ?? null;
      }
      return firstFilePath(treePaths);
    });
  }, [entriesByPath, treePaths]);

  // Report file selections upward for URL sync. Nulls (transient resets)
  // are never reported — they would wipe a live ?file= anchor.
  const onSelectedFileChangeRef = useRef(onSelectedFileChange);
  useEffect(() => {
    onSelectedFileChangeRef.current = onSelectedFileChange;
  });
  useEffect(() => {
    if (selectedPath === null) return;
    const file = entriesByPath.get(selectedPath);
    onSelectedFileChangeRef.current?.(selectedPath, file?.fullPath);
    // entriesByPath is a dependency only to read fullPath; a listing refresh
    // re-reports the same selection, a no-op upstream.
  }, [selectedPath, entriesByPath]);

  const selectedFile =
    selectedPath != null ? (entriesByPath.get(selectedPath) ?? null) : null;

  if (isLoading) {
    return (
      <div className="space-y-2 p-4">
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-3/4" />
      </div>
    );
  }

  if (error) {
    if (errorStatus === 404) {
      return (
        <div className="p-6 text-center">
          <Package className="text-muted-foreground/50 mx-auto mb-2 h-8 w-8" />
          <p className="text-muted-foreground text-sm">
            Artifacts are unavailable for this attempt.
          </p>
          {successfulAnalysisTrial && (
            <p className="mt-1 text-xs text-red-500">
              This successful analysis run promised durable artifacts; an
              integrity alert was recorded.
            </p>
          )}
        </div>
      );
    }
    if (errorStatus === 403) {
      return (
        <div className="p-6 text-center">
          <p className="text-sm text-red-500">
            You are not authorized to view artifacts for this trial.
          </p>
        </div>
      );
    }
    return (
      <div className="p-6 text-center">
        <p className="text-muted-foreground text-sm">
          Could not load artifacts.
        </p>
        <Button
          type="button"
          variant="secondary"
          size="sm"
          className="mt-3 h-7"
          onClick={() => void mutate()}
          disabled={isValidating}
        >
          {isValidating ? (
            <>
              <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
              Retrying…
            </>
          ) : (
            "Retry"
          )}
        </Button>
      </div>
    );
  }

  if (entriesByPath.size === 0) {
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

  const fileCountLabel = `${entriesByPath.size} ${
    entriesByPath.size === 1 ? "file" : "files"
  }`;

  return (
    <div className="@container/file-browser flex h-full min-h-0 min-w-0 overflow-hidden">
      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden @2xl/file-browser:flex-row">
        {/* Stacked layout needs a definite height: FileTreePane virtualizes
            against this viewport. Once the viewer's own container is wide
            enough for a row, the tree stretches to the full pane height. */}
        <div className="border-border bg-muted/30 flex h-[30vh] w-full flex-col overflow-hidden border-b p-2 @2xl/file-browser:h-auto @2xl/file-browser:w-56 @2xl/file-browser:shrink-0 @2xl/file-browser:border-r @2xl/file-browser:border-b-0 @3xl/file-browser:w-64">
          <div className="text-muted-foreground flex items-center justify-between gap-2 px-2 py-2 font-mono text-[10px] font-semibold tracking-wide uppercase sm:text-xs">
            <span>Artifacts</span>
            <span className="text-muted-foreground/70 font-sans text-[10px] font-normal normal-case">
              {fileCountLabel}
            </span>
          </div>
          <FileTreePane
            className="min-h-0 flex-1"
            onSelectPath={setSelectedPath}
            paths={treePaths}
            selectedPath={selectedPath}
          />
        </div>
        <ArtifactContentPane
          filesUrl={filesUrl}
          selectedFile={selectedFile}
          viewMode={viewMode}
          onViewModeChange={setViewMode}
          selectedLines={selectedLines}
          onSelectLinesChange={onSelectLinesChange}
        />
      </div>
    </div>
  );
}

interface ArtifactContentPaneProps {
  filesUrl: string;
  selectedFile: ArtifactEntry | null;
  viewMode: "rendered" | "raw";
  onViewModeChange: (mode: "rendered" | "raw") => void;
  selectedLines?: LineRange | null;
  onSelectLinesChange?: (range: LineRange | null) => void;
}

function ArtifactContentPane({
  filesUrl,
  selectedFile,
  viewMode,
  onViewModeChange,
  selectedLines,
  onSelectLinesChange,
}: ArtifactContentPaneProps) {
  const contentRef = useRef<HTMLDivElement>(null);
  const [content, setContent] = useState<string | null>(null);
  const [contentLoading, setContentLoading] = useState(false);
  const [contentError, setContentError] = useState<string | null>(null);
  const [isTruncated, setIsTruncated] = useState(false);
  const [loadingFullFile, setLoadingFullFile] = useState(false);

  const fullPath = selectedFile?.fullPath ?? null;
  const presignedUrl = selectedFile?.url;
  const fileSize = selectedFile?.size;
  const fileName = selectedFile?.path.split("/").pop() ?? "";
  const isBinary = fileName ? isBinaryRendererFile(fileName) : false;

  // Each path segment is URL-encoded individually so `/` separators in the
  // path stay intact for the backend file route (encodeURIComponent would
  // turn them into %2F and miss the route).
  const proxyUrl = useMemo(() => {
    if (!fullPath) return null;
    const encoded = fullPath.split("/").map(encodeURIComponent).join("/");
    return `${filesUrl}/${encoded}`;
  }, [filesUrl, fullPath]);

  useEffect(() => {
    if (contentRef.current) contentRef.current.scrollTop = 0;
  }, [selectedFile?.path]);

  useEffect(() => {
    if (!selectedFile || !proxyUrl) {
      setContent(null);
      setContentLoading(false);
      setContentError(null);
      setIsTruncated(false);
      return;
    }
    if (isBinary) {
      setContent(null);
      setContentLoading(false);
      setContentError(null);
      setIsTruncated(false);
      return;
    }

    const shouldTruncate =
      typeof fileSize === "number" && fileSize > TRUNCATE_THRESHOLD;
    let cancelled = false;
    setContentLoading(true);
    setContentError(null);

    async function fetchText() {
      try {
        let text: string | null = null;
        let truncated = false;

        if (presignedUrl) {
          try {
            const headers: HeadersInit = shouldTruncate
              ? { Range: `bytes=0-${TRUNCATE_THRESHOLD - 1}` }
              : {};
            const res = await fetch(presignedUrl, { headers });
            if (res.ok || res.status === 206) {
              text = await res.text();
              truncated =
                res.status === 206 ||
                (!!shouldTruncate && text.length >= TRUNCATE_THRESHOLD);
            }
          } catch {
            // fall through to proxy
          }
        }

        if (text === null) {
          const res = await fetch(proxyUrl!);
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          text = await res.text();
          truncated = !!shouldTruncate && text.length >= TRUNCATE_THRESHOLD;
        }

        if (!cancelled) {
          setContent(text ?? "");
          setIsTruncated(truncated);
        }
      } catch (err) {
        if (!cancelled) {
          setContentError(
            err instanceof Error ? err.message : "Failed to load file"
          );
          setContent("");
          setIsTruncated(false);
        }
      } finally {
        if (!cancelled) setContentLoading(false);
      }
    }

    void fetchText();
    return () => {
      cancelled = true;
    };
  }, [selectedFile, proxyUrl, presignedUrl, isBinary, fileSize]);

  const loadFullFile = useCallback(async () => {
    if (!selectedFile || !proxyUrl) return;
    setLoadingFullFile(true);
    try {
      if (presignedUrl) {
        try {
          const res = await fetch(presignedUrl);
          if (res.ok) {
            const text = await res.text();
            setContent(text);
            setIsTruncated(false);
            return;
          }
        } catch {
          // fall through to proxy
        }
      }
      const res = await fetch(proxyUrl);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const text = await res.text();
      setContent(text);
      setIsTruncated(false);
    } catch (err) {
      setContentError(
        err instanceof Error ? err.message : "Failed to load full file"
      );
    } finally {
      setLoadingFullFile(false);
    }
  }, [selectedFile, proxyUrl, presignedUrl]);

  if (!selectedFile) {
    return (
      <div className="text-muted-foreground flex h-full flex-1 items-center justify-center text-sm">
        Select a file to view its contents
      </div>
    );
  }

  const renderUrl = presignedUrl || proxyUrl || null;

  return (
    <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
      <div className="border-border bg-muted/30 flex items-center justify-between gap-2 border-b px-3 py-2 sm:px-4">
        <div className="text-muted-foreground min-w-0 flex-1 truncate font-mono text-[10px] sm:text-xs">
          {selectedFile.path}
          {typeof fileSize === "number" && (
            <span className="text-muted-foreground/70 ml-2">
              ({formatFileSize(fileSize)})
            </span>
          )}
        </div>
        {!isBinary && (
          <Tabs
            value={viewMode}
            onValueChange={(v) => onViewModeChange(v as "rendered" | "raw")}
          >
            <TabsList className="h-7">
              <TabsTrigger value="rendered" className="h-6 px-2 text-[10px]">
                <Eye className="mr-1 h-3 w-3" />
                Rendered
              </TabsTrigger>
              <TabsTrigger value="raw" className="h-6 px-2 text-[10px]">
                <Code className="mr-1 h-3 w-3" />
                Raw
              </TabsTrigger>
            </TabsList>
          </Tabs>
        )}
      </div>
      <div ref={contentRef} className="bg-card flex-1 overflow-auto">
        {contentLoading ? (
          <div className="space-y-2 p-4">
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-3/4" />
            <Skeleton className="h-4 w-5/6" />
          </div>
        ) : contentError && !isBinary ? (
          <div className="text-destructive p-4 text-sm">
            Failed to load {fileName}: {contentError}
          </div>
        ) : (
          <FileRenderer
            fileName={fileName}
            url={renderUrl}
            content={isBinary ? null : content}
            fileSize={fileSize}
            viewMode={viewMode}
            selectedLines={selectedLines}
            onSelectLines={onSelectLinesChange}
          />
        )}
      </div>
      {!isBinary && isTruncated && (
        <div className="border-border bg-muted/50 flex items-center justify-between border-t px-4 py-3">
          <span className="text-muted-foreground text-xs">
            Showing first {formatFileSize(TRUNCATE_THRESHOLD)} of{" "}
            {fileSize ? formatFileSize(fileSize) : "large file"}
          </span>
          <Button
            type="button"
            size="sm"
            onClick={loadFullFile}
            disabled={loadingFullFile}
            className="h-auto px-3 py-1.5 text-xs"
          >
            {loadingFullFile ? (
              <>
                <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                Loading...
              </>
            ) : (
              "Load full file"
            )}
          </Button>
        </div>
      )}
    </div>
  );
}
