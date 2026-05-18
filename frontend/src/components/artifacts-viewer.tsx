"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  ChevronDown,
  ChevronRight,
  Code,
  Eye,
  File,
  FileCode,
  FileText,
  Folder,
  FolderOpen,
  Loader2,
  Package,
} from "lucide-react";
import {
  FileRenderer,
  isBinaryRendererFile,
} from "@/components/renderers/file-renderer";
import { fetcher } from "@/lib/api";

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

interface TreeNode {
  name: string;
  // Relative path inside the synthetic artifact root. Used as the React key
  // and for selection state — stripped of the Harbor `<trial_name>/` (and
  // `steps/<step>/`) wrapper dirs so the tree reads like a normal filesystem.
  path: string;
  // Original S3-relative path returned by /trials/{id}/files. Used to build
  // the backend proxy URL for content fetches.
  fullPath?: string;
  type: "file" | "dir";
  size?: number;
  url?: string;
  children?: TreeNode[];
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

function getFileIcon(name: string) {
  const ext = name.split(".").pop()?.toLowerCase();
  switch (ext) {
    case "md":
    case "txt":
    case "log":
      return FileText;
    case "ts":
    case "tsx":
    case "js":
    case "jsx":
    case "py":
    case "toml":
    case "yaml":
    case "yml":
    case "sh":
    case "json":
      return FileCode;
    default:
      return File;
  }
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function buildArtifactTree(files: ArtifactFile[]): TreeNode[] {
  const dirIndex = new Map<string, TreeNode>();
  const roots: TreeNode[] = [];

  function ensureDir(parts: string[]): TreeNode | null {
    if (parts.length === 0) return null;
    const dirPath = parts.join("/");
    const existing = dirIndex.get(dirPath);
    if (existing) return existing;
    const parent = ensureDir(parts.slice(0, -1));
    const node: TreeNode = {
      name: parts[parts.length - 1],
      path: dirPath,
      type: "dir",
      children: [],
    };
    dirIndex.set(dirPath, node);
    if (parent) parent.children!.push(node);
    else roots.push(node);
    return node;
  }

  for (const file of files) {
    const rel = relativizeArtifactPath(file.path);
    const parts = rel.split("/").filter(Boolean);
    if (parts.length === 0) continue;
    const fileName = parts[parts.length - 1];
    const parentDir = parts.length > 1 ? ensureDir(parts.slice(0, -1)) : null;
    const fileNode: TreeNode = {
      name: fileName,
      path: rel,
      fullPath: file.path,
      type: "file",
      size: file.size,
      url: file.url,
    };
    if (parentDir) parentDir.children!.push(fileNode);
    else roots.push(fileNode);
  }

  function sortChildren(nodes: TreeNode[]) {
    nodes.sort((a, b) => {
      if (a.type !== b.type) return a.type === "dir" ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    for (const node of nodes) {
      if (node.children) sortChildren(node.children);
    }
  }
  sortChildren(roots);
  return roots;
}

function findFirstFile(nodes: TreeNode[]): TreeNode | null {
  for (const node of nodes) {
    if (node.type === "file") return node;
    if (node.type === "dir" && node.children) {
      const found = findFirstFile(node.children);
      if (found) return found;
    }
  }
  return null;
}

function collectDirPaths(nodes: TreeNode[]): string[] {
  const paths: string[] = [];
  for (const node of nodes) {
    if (node.type === "dir") {
      paths.push(node.path);
      if (node.children) paths.push(...collectDirPaths(node.children));
    }
  }
  return paths;
}

function collectFiles(nodes: TreeNode[]): TreeNode[] {
  const out: TreeNode[] = [];
  for (const node of nodes) {
    if (node.type === "file") out.push(node);
    else if (node.children) out.push(...collectFiles(node.children));
  }
  return out;
}

interface ArtifactsViewerProps {
  filesUrl: string;
}

export function ArtifactsViewer({ filesUrl }: ArtifactsViewerProps) {
  const { data, isLoading, error } = useSWR<ArtifactsListing>(
    `${filesUrl}?recursive=1`,
    fetcher,
    { revalidateOnFocus: false },
  );

  const tree = useMemo(() => {
    const artifactFiles = (data?.files ?? []).filter((f) =>
      isArtifactPath(f.path),
    );
    return buildArtifactTree(artifactFiles);
  }, [data]);

  const allFiles = useMemo(() => collectFiles(tree), [tree]);

  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [expandedDirs, setExpandedDirs] = useState<Set<string>>(new Set());
  const [viewMode, setViewMode] = useState<"rendered" | "raw">("rendered");

  // First load: expand every dir and select the first file. We also re-run
  // this if the file set changes (e.g. trial finishes producing artifacts
  // while the drawer is open) but only fall back to a fresh selection when
  // the previously selected path no longer exists.
  useEffect(() => {
    if (!tree.length) {
      setSelectedPath(null);
      setExpandedDirs(new Set());
      return;
    }
    setExpandedDirs(new Set(collectDirPaths(tree)));
    setSelectedPath((prev) => {
      if (prev && allFiles.some((f) => f.path === prev)) return prev;
      const first = findFirstFile(tree);
      return first?.path ?? null;
    });
  }, [tree, allFiles]);

  const selectedFile = useMemo(
    () => allFiles.find((f) => f.path === selectedPath) ?? null,
    [allFiles, selectedPath],
  );

  const toggleDir = useCallback((path: string) => {
    setExpandedDirs((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  const renderTree = (nodes: TreeNode[], depth = 0): ReactNode => {
    return nodes.map((node) => {
      const isExpanded = expandedDirs.has(node.path);
      const isSelected = selectedFile?.path === node.path;
      const Icon =
        node.type === "dir"
          ? isExpanded
            ? FolderOpen
            : Folder
          : getFileIcon(node.name);
      return (
        <div key={node.path}>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => {
              if (node.type === "dir") toggleDir(node.path);
              else setSelectedPath(node.path);
            }}
            className={`h-auto w-full justify-start gap-1.5 rounded px-2 py-1 text-left font-mono text-xs transition-colors ${
              isSelected
                ? "bg-primary/20 text-primary hover:bg-primary/20"
                : "text-foreground hover:bg-muted"
            }`}
            style={{ paddingLeft: `${depth * 12 + 8}px` }}
          >
            {node.type === "dir" ? (
              <span className="flex h-3 w-3 items-center justify-center">
                {isExpanded ? (
                  <ChevronDown className="text-muted-foreground h-3 w-3" />
                ) : (
                  <ChevronRight className="text-muted-foreground h-3 w-3" />
                )}
              </span>
            ) : (
              <span className="w-3" />
            )}
            <Icon
              className={`h-4 w-4 shrink-0 ${
                node.type === "dir"
                  ? "text-yellow-500"
                  : "text-muted-foreground"
              }`}
            />
            <span className="truncate">{node.name}</span>
          </Button>
          {node.type === "dir" && isExpanded && node.children && (
            <div>{renderTree(node.children, depth + 1)}</div>
          )}
        </div>
      );
    });
  };

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

  if (allFiles.length === 0) {
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

  const fileCountLabel = `${allFiles.length} ${
    allFiles.length === 1 ? "file" : "files"
  }`;

  return (
    <div className="flex h-full flex-col overflow-hidden md:flex-row">
      <div className="border-border bg-muted/30 max-h-[30vh] w-full overflow-auto border-b md:max-h-none md:w-56 md:border-r md:border-b-0 lg:w-64">
        <div className="p-2">
          <div className="text-muted-foreground flex items-center justify-between gap-2 px-2 py-2 font-mono text-[10px] font-semibold tracking-wide uppercase sm:text-xs">
            <span>Artifacts</span>
            <span className="text-muted-foreground/70 font-sans text-[10px] font-normal normal-case">
              {fileCountLabel}
            </span>
          </div>
          {renderTree(tree)}
        </div>
      </div>
      <ArtifactContentPane
        filesUrl={filesUrl}
        selectedFile={selectedFile}
        viewMode={viewMode}
        onViewModeChange={setViewMode}
      />
    </div>
  );
}

interface ArtifactContentPaneProps {
  filesUrl: string;
  selectedFile: TreeNode | null;
  viewMode: "rendered" | "raw";
  onViewModeChange: (mode: "rendered" | "raw") => void;
}

function ArtifactContentPane({
  filesUrl,
  selectedFile,
  viewMode,
  onViewModeChange,
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
  const fileName = selectedFile?.name ?? "";
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
