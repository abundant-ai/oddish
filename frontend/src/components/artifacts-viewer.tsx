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
import type { LineRange } from "@/lib/line-range";

// Truncate previews of files larger than 100KB so we don't blow up the
// renderer pane on huge artifacts (matches TaskFilesPanel).
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
  // Relative path inside the synthetic artifact root. Identifies the row in
  // the tree and drives selection state — stripped of the Harbor
  // `<trial_name>/` (and `steps/<step>/`) wrapper dirs so the tree reads like
  // a normal filesystem.
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
 * Flattens the listing into one entry per artifact file, keyed by
 * relativized path. Directory rows are inferred from the path segments by
 * @pierre/trees, so only leaves are built here — and `path` is the tree's
 * identity, so colliding relativized paths (a multi-step and a single-step
 * artifact reducing to the same name) keep the first entry rather than
 * producing a duplicate row.
 */
function buildArtifactEntries(
  files: ArtifactFile[],
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
  initialFilePath,
  selectedLines,
  onSelectLinesChange,
  onSelectedFileChange,
}: ArtifactsViewerProps) {
  const { data, isLoading, error } = useSWR<ArtifactsListing>(
    `${filesUrl}?recursive=1`,
    fetcher,
    { revalidateOnFocus: false },
  );

  const entriesByPath = useMemo(
    () =>
      buildArtifactEntries(
        (data?.files ?? []).filter((f) => isArtifactPath(f.path)),
      ),
    [data],
  );

  // The tree takes a flat path list and infers the directories. Memoized so
  // its identity only changes when the listing does — FileTreePane rebuilds
  // (and re-expands) the tree on every new array.
  const treePaths = useMemo(() => [...entriesByPath.keys()], [entriesByPath]);

  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<"rendered" | "raw">("rendered");

  // A deep-linked path owns the first selection; read through a ref so the
  // load effect doesn't re-run when the parent echoes selections back.
  const initialFilePathRef = useRef(initialFilePath);
  useEffect(() => {
    initialFilePathRef.current = initialFilePath;
  });

  // First load: select the deep-linked file if one is addressed (exact path
  // or suffix), else the first file. We also re-run this if the file set
  // changes (e.g. trial finishes producing artifacts while the drawer is
  // open) but only fall back to a fresh selection when the previously
  // selected path no longer exists.
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
    // entriesByPath is a dependency only to read the fullPath; a listing
    // refresh re-reports the same selection, which the parent treats as a
    // no-op.
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
    return (
      <div className="text-muted-foreground p-6 text-center text-sm">
        Failed to load artifacts
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
    <div className="flex h-full flex-col overflow-hidden md:flex-row">
      <div className="border-border bg-muted/30 flex max-h-[30vh] w-full flex-col overflow-hidden border-b p-2 md:max-h-none md:w-56 md:border-r md:border-b-0 lg:w-64">
        <div className="text-muted-foreground flex items-center justify-between gap-2 px-2 py-2 font-mono text-[10px] font-semibold tracking-wide uppercase sm:text-xs">
          <span>Artifacts</span>
          <span className="text-muted-foreground/70 font-sans text-[10px] font-normal normal-case">
            {fileCountLabel}
          </span>
        </div>
        {/* The tree virtualizes its own rows, so it scrolls internally and
            needs a bounded height rather than an `overflow-auto` parent. */}
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

  // Scroll back to the top when the selected file changes so the user
  // doesn't land halfway through a file's content.
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
            err instanceof Error ? err.message : "Failed to load file",
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
        err instanceof Error ? err.message : "Failed to load full file",
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
    <div className="flex flex-1 flex-col overflow-hidden">
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
