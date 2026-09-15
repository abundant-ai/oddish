"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { useTaskFileTree } from "@/lib/use-task-file-tree";
import {
  FILE_PREVIEW_BYTES as TRUNCATE_THRESHOLD,
  fetchTrialFilePreview,
  trialFilePreviewKey,
  useFileCacheScope,
} from "@/lib/file-resources";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Code, Eye, Loader2, Package } from "lucide-react";
import { FileTreePane } from "@/components/file-tree-pane";
import {
  FileRenderer,
  isBinaryRendererFile,
} from "@/components/renderers/file-renderer";
import { apiFetch } from "@/lib/api";
import { firstFilePath } from "@/lib/file-tree-order";
import { formatFileSize } from "@/lib/format";
import { encodeFilePath, sameFilePath } from "@/lib/file-path";
import { recordClientError } from "@/lib/observability";
import type { LineRange } from "@/lib/line-range";

interface ArtifactFile {
  path: string;
  key?: string;
  size?: number;
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
    });
  }
  return entries;
}

interface ArtifactsViewerProps {
  isActive?: boolean;
  trialAttempt?: number;
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
  isActive = true,
  trialAttempt = 0,
  filesUrl,
  trialId,
  successfulAnalysisTrial = false,
  initialFilePath,
  selectedLines,
  onSelectLinesChange,
  onSelectedFileChange,
}: ArtifactsViewerProps) {
  const scope = useFileCacheScope(filesUrl);
  const inventory = useTaskFileTree({
    enabled: true,
    active: isActive,
    url: filesUrl,
    version: null,
    hash: null,
    attempt: trialAttempt,
    artifacts: true,
  });
  const {
    isLoading,
    isValidating,
    error,
    refresh,
    loadDirectory,
    onFileError,
  } = inventory;
  const page = inventory.data?.directories[""];
  const revision = inventory.data?.source_hash ?? null;
  const paginationError = inventory.statusByDirectory[""] === "error";
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
    () => buildArtifactEntries(page?.files ?? []),
    [page?.files]
  );

  // Identity must only change with the listing — FileTreePane rebuilds (and
  // re-expands) the tree on any new array.
  const treePaths = useMemo(() => [...entriesByPath.keys()], [entriesByPath]);

  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<"rendered" | "raw">("rendered");

  const wanted = selectedPath ?? initialFilePath;
  const entries = [...entriesByPath.values()];
  const selectedFile = wanted
    ? (entriesByPath.get(wanted) ??
      entries.find(
        (file) =>
          file.fullPath === wanted ||
          sameFilePath(file.path, wanted) ||
          sameFilePath(file.fullPath, wanted)
      ) ??
      null)
    : (entriesByPath.get(firstFilePath(treePaths) ?? "") ?? null);
  const effectiveSelectedPath = selectedFile?.path ?? null;

  useEffect(() => {
    if (
      initialFilePath &&
      !selectedFile &&
      isActive &&
      page?.cursor &&
      !isValidating &&
      !error &&
      !inventory.statusByDirectory[""]
    ) {
      void loadDirectory("", page.cursor);
    }
  }, [
    initialFilePath,
    selectedFile,
    page,
    isActive,
    inventory.statusByDirectory,
    isValidating,
    error,
    loadDirectory,
  ]);

  // Report file selections upward for URL sync. Nulls (transient resets)
  // are never reported — they would wipe a live ?file= anchor.
  const onSelectedFileChangeRef = useRef(onSelectedFileChange);
  useEffect(() => {
    onSelectedFileChangeRef.current = onSelectedFileChange;
  });
  useEffect(() => {
    if (effectiveSelectedPath === null) return;
    const file = entriesByPath.get(effectiveSelectedPath);
    onSelectedFileChangeRef.current?.(effectiveSelectedPath, file?.fullPath);
    // entriesByPath is a dependency only to read fullPath; a listing refresh
    // re-reports the same selection, a no-op upstream.
  }, [effectiveSelectedPath, entriesByPath]);

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
          onClick={() => void refresh()}
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
            selectedPath={effectiveSelectedPath}
          />
          {page?.cursor && (
            <Button
              variant="ghost"
              size="sm"
              disabled={
                isValidating || inventory.statusByDirectory[""] === "loading"
              }
              onClick={() => void loadDirectory("", page.cursor)}
            >
              {paginationError
                ? "Retry loading artifacts"
                : "Load more artifacts"}
            </Button>
          )}
        </div>
        <ArtifactContentPane
          key={JSON.stringify([
            scope,
            filesUrl,
            trialAttempt,
            revision,
            selectedFile?.fullPath,
          ])}
          trialAttempt={trialAttempt}
          revision={revision}
          onFileError={onFileError}
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
  onFileError: (error: { status?: number }) => Promise<void>;
  revision: string | null;
  trialAttempt: number;
  filesUrl: string;
  selectedFile: ArtifactEntry | null;
  viewMode: "rendered" | "raw";
  onViewModeChange: (mode: "rendered" | "raw") => void;
  selectedLines?: LineRange | null;
  onSelectLinesChange?: (range: LineRange | null) => void;
}

function ArtifactContentPane({
  onFileError,
  revision,
  trialAttempt,
  filesUrl,
  selectedFile,
  viewMode,
  onViewModeChange,
  selectedLines,
  onSelectLinesChange,
}: ArtifactContentPaneProps) {
  const contentRef = useRef<HTMLDivElement>(null);
  const { mutate } = useSWRConfig();
  const [fullError, setFullError] = useState<string | null>(null);
  const [loadingFullFile, setLoadingFullFile] = useState(false);
  const scope = useFileCacheScope(filesUrl);
  const fullPath = selectedFile?.fullPath ?? null;
  const fileSize = selectedFile?.size;
  const fileName = selectedFile?.path.split("/").pop() ?? "";
  const isBinary = fileName ? isBinaryRendererFile(fileName) : false;

  const proxyUrl = fullPath ? `${filesUrl}/${encodeFilePath(fullPath)}` : null;

  useEffect(() => {
    if (contentRef.current) contentRef.current.scrollTop = 0;
  }, [selectedFile?.path]);

  const previewKey =
    fullPath && !isBinary
      ? trialFilePreviewKey(scope, filesUrl, fullPath, trialAttempt, revision)
      : null;
  const {
    data: preview,
    error: previewError,
    isLoading: contentLoading,
  } = useSWR(previewKey, fetchTrialFilePreview, {
    revalidateOnFocus: false,
    revalidateIfStale: false,
    shouldRetryOnError: false,
    onError: onFileError,
  });
  const content = preview?.content ?? null;
  const contentError = fullError ?? previewError?.message ?? null;
  const isTruncated = preview?.isTruncated ?? false;

  async function loadFullFile() {
    if (!selectedFile || !proxyUrl || !previewKey || !preview) return;
    setLoadingFullFile(true);
    try {
      const res = await apiFetch(
        `${proxyUrl}?indexed=true&attempt=${trialAttempt}&revision=${encodeURIComponent(previewKey[5])}`
      );
      if (!res.ok)
        throw Object.assign(new Error(`HTTP ${res.status}`), {
          status: res.status,
        });
      const text = await res.text();
      await mutate(
        previewKey,
        { ...preview, content: text, isTruncated: false },
        { revalidate: false }
      );
      setFullError(null);
    } catch (err) {
      await onFileError(err as { status?: number });
      setFullError(
        err instanceof Error ? err.message : "Failed to load full file"
      );
    } finally {
      setLoadingFullFile(false);
    }
  }

  if (!selectedFile) {
    return null;
  }

  const renderUrl =
    proxyUrl && revision
      ? `${proxyUrl}?indexed=true&attempt=${trialAttempt}&revision=${encodeURIComponent(revision)}`
      : null;

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
