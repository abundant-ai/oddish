"use client";

import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import useSWR from "swr";
import {
  ResizableDrawer,
  DrawerHeader,
  DrawerTitle,
} from "@/components/ui/resizable-drawer";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Folder,
  FolderOpen,
  File,
  FileText,
  FileCode,
  ChevronRight,
  ChevronDown,
  ChevronUp,
  RefreshCw,
  AlertCircle,
  ListChecks,
  Microscope,
  Loader2,
  OctagonX,
  Eye,
  Code,
  Copy,
  Check,
  Lightbulb,
  Package,
  Wrench,
} from "lucide-react";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { fetcher } from "@/lib/api";
import { formatFileSize } from "@/lib/format";
import {
  buildTaskFileSections,
  taskFileSectionIdForRootNode,
  type TaskFileSectionId,
} from "@/lib/task-file-sections";
import type { LineRange } from "@/lib/line-range";
import {
  FileRenderer,
  isBinaryRendererFile,
} from "@/components/renderers/file-renderer";
import type {
  Task,
  TaskDetailResponse,
  TaskVersionSummary,
  Trial,
} from "@/lib/types";
import { isAgentTrial } from "@/lib/types";
import {
  isBrowseTaskDetail,
  taskDetailKey,
  taskDetailValue,
  type TaskDetailResource,
} from "@/lib/task-detail-resource";
import { TaskOverviewPanel } from "@/components/task-overview-panel";
import {
  getCancelActionLabel,
  isActivePipelineStatus,
  taskHasActiveAnalysis,
  taskHasActiveTrials,
  taskHasActiveVerdict,
  taskHasCancellableWork,
  taskHasLiveAnalysisTrial,
} from "@/lib/job-status";

interface TaskFile {
  path: string;
  key: string;
  content?: string;
  size?: number;
  last_modified?: string;
  url?: string; // Presigned S3 URL for direct access
}

interface FilesListingResponse {
  files?: TaskFile[];
  dirs?: Array<{ path: string }>;
  cursor?: string | null;
}

/**
 * Chunks of the NDJSON listing stream: the bare tree first, then file
 * bodies as the backend loads them (shallowest files first).
 */
type FilesStreamChunk =
  | ({ type: "listing" } & FilesListingResponse)
  | { type: "content"; path: string; content: string };

async function* iterateNdjsonLines(
  body: ReadableStream<Uint8Array>
): AsyncGenerator<unknown> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let newline = buffer.indexOf("\n");
      while (newline >= 0) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (line) yield JSON.parse(line);
        newline = buffer.indexOf("\n");
      }
    }
    const rest = (buffer + decoder.decode()).trim();
    if (rest) yield JSON.parse(rest);
  } finally {
    reader.releaseLock();
  }
}

interface TreeNode {
  name: string;
  path: string;
  type: "file" | "dir";
  children?: TreeNode[];
  content?: string;
  url?: string; // Presigned S3 URL for direct access
  size?: number; // File size in bytes
}

const TASK_FILE_SECTION_ICONS = {
  prompt: FileText,
  solution: Lightbulb,
  verification: Microscope,
  environment: Package,
  metadata: FileCode,
  tooling: Wrench,
  other: FolderOpen,
} satisfies Record<TaskFileSectionId, typeof File>;

interface DirectoryListing {
  nodes: TreeNode[];
  cursor: string | null;
  status: "ready" | "loading" | "error";
}

type FilePreview =
  | { kind: "text"; content: string; isTruncated: boolean; size: number | null }
  | { kind: "binary"; url: string; size: number | null };

interface TaskFilesPanelProps {
  isOpen: boolean;
  onClose: () => void;
  taskId: string | null;
  task?: Task | null;
  orderedTasks?: Task[] | null;
  taskIndex?: number | null;
  onNavigate?: (task: Task, taskIndex: number) => void;
  onNavigateToFirstTrial?: () => void;
  apiBaseUrl?: string;
  /**
   * When set, Cancel only stops trials belonging to this experiment. Used by
   * the experiment drawer so shared tasks keep running elsewhere. Omit on the
   * task page to cancel every in-flight trial for the task.
   */
  cancelExperimentId?: string;
  allowRetry?: boolean;
  /**
   * When false, analysis/verdict UI (the verdict badge and the run
   * analysis/verdict actions) is hidden entirely — used by the public
   * read-only share view.
   */
  showAnalysis?: boolean;
  /** Whether the task drawer offers and may fetch the capability analysis. */
  /** The route host owns this because the drawer can mount two task panes. */
  activePane: TaskPane;
  onActivePaneChange?: (pane: TaskPane) => void;
  onRetryComplete?: (taskIds?: string[]) => void;
  /** Render content only without ResizableDrawer wrapper */
  contentOnly?: boolean;
  /**
   * Override the files URL base (e.g. `/api/trials/{id}/files`).
   * When set, the component fetches directory listings from `${filesUrl}`
   * and individual file content from `${filesUrl}/${path}`.
   * This allows reusing the file tree viewer for trial files.
   */
  filesUrl?: string;
  /** Load only file metadata up front, then fetch bodies or URLs on selection. */
  loadFilesLazily?: boolean;
  /** Explicit task version for file URLs; null deliberately means unversioned. */
  taskVersion?: number | null;
  /**
   * When set, auto-expand the tree to this file path and select it.
   * Useful for deep-linking from external UI (e.g. execution timeline).
   * Bump the value or pair with a counter to re-trigger navigation to the same path.
   */
  initialFilePath?: string | null;
  /**
   * Task id to source the TASK OVERVIEW entry from, for panes that drive file
   * listing via `filesUrl` and pass `taskId={null}` (e.g. the side-by-side
   * "Task definition" pane). Falls back to `taskId` when not set.
   */
  staticChecksTaskId?: string | null;
  /** Detail already owned by the host page; avoids re-fetching the same key. */
  taskDetail?: TaskDetailResource | null;
  /**
   * Open a trial from the overview's aggregated QA in the caller's own
   * context (drawer / panel). Return false when the trial isn't addressable
   * there; the overview then falls back to the task page deep link.
   */
  onOpenTrial?: (trial: Trial) => boolean;
  /**
   * The host is still streaming `task.trials` (progressive experiment
   * loading) — the overview renders an empty scope as loading, not as
   * "no trials".
   */
  overviewTrialsLoading?: boolean;
  /**
   * Line range to highlight in the selected file — the ``?lines=L12-L20``
   * deep-link anchor. Honored by line-oriented renderers only.
   */
  selectedLines?: LineRange | null;
  /** Line selection changes from the file viewer, for URL sync. */
  onSelectLinesChange?: (range: LineRange | null) => void;
  /**
   * Reports the selected file's path whenever a file is selected (tree
   * clicks and auto-selection alike), so callers can keep ``?file=`` live
   * and drop a stale ``?lines=`` when the file changes. Never called with
   * null — transient resets (listing reloads, close) are not reported.
   */
  onSelectedFileChange?: (path: string) => void;
}

export type TaskPane = "overview" | "file";

function getNodeName(path: string): string {
  const parts = path.split("/").filter(Boolean);
  return parts[parts.length - 1] || path;
}

/** The version whose static checks the pane shows: the pinned version when
 *  the pane is scoped to one (the experiment drawer), else current, else
 *  newest. /detail orders versions newest-first, so the fallback is
 *  versions[0]. */
function pickChecksVersion(
  detail: TaskDetailResponse | undefined,
  pinnedVersion?: number | null
): TaskVersionSummary | null {
  const versions = detail?.versions;
  if (!versions || versions.length === 0) return null;
  if (pinnedVersion != null) {
    const pinned = versions.find((v) => v.version === pinnedVersion);
    if (pinned) return pinned;
  }
  return versions.find((v) => v.is_current) ?? versions[0];
}

// Truncate files larger than 100KB initially
const TRUNCATE_THRESHOLD = 100 * 1024;
const FILE_LOAD_ERROR = "Error loading file content";

/**
 * Build the full nested tree from a recursive listing in one pass.
 * Directories are implied by nested file paths, so expanding them is
 * pure UI state — no per-directory round trips.
 */
function buildTreeFromListing(files: TaskFile[] = []): TreeNode[] {
  const root: TreeNode[] = [];
  const dirNodes = new Map<string, TreeNode>();

  const ensureDir = (path: string): TreeNode => {
    const existing = dirNodes.get(path);
    if (existing) return existing;
    const node: TreeNode = {
      name: getNodeName(path),
      path,
      type: "dir",
      children: [],
    };
    dirNodes.set(path, node);
    const parentPath = path.split("/").slice(0, -1).join("/");
    (parentPath ? ensureDir(parentPath).children! : root).push(node);
    return node;
  };

  for (const file of files) {
    const node: TreeNode = {
      name: getNodeName(file.path),
      path: file.path,
      type: "file",
      content: file.content,
      url: file.url,
      size: file.size,
    };
    const parentPath = file.path.split("/").slice(0, -1).join("/");
    (parentPath ? ensureDir(parentPath).children! : root).push(node);
  }

  const sortLevel = (nodes: TreeNode[]) => {
    nodes.sort((a, b) =>
      a.type === b.type
        ? a.name.localeCompare(b.name)
        : a.type === "dir"
          ? -1
          : 1
    );
    for (const node of nodes) {
      if (node.children && node.children.length > 0) sortLevel(node.children);
    }
  };
  sortLevel(root);
  return root;
}

function sortTreeLevel(nodes: TreeNode[]): TreeNode[] {
  return [...nodes].sort((a, b) =>
    a.type === b.type ? a.name.localeCompare(b.name) : a.type === "dir" ? -1 : 1
  );
}

function buildDirectoryPage(listing: FilesListingResponse): TreeNode[] {
  const dirs = (listing.dirs ?? []).map((dir) => ({
    name: getNodeName(dir.path),
    path: dir.path,
    type: "dir" as const,
  }));
  const files = (listing.files ?? []).map((file) => ({
    name: getNodeName(file.path),
    path: file.path,
    type: "file" as const,
    content: file.content,
    url: file.url,
    size: file.size,
  }));
  return sortTreeLevel([...dirs, ...files]);
}

function mergeTreeLevel(current: TreeNode[], incoming: TreeNode[]): TreeNode[] {
  const byPath = new Map(current.map((node) => [node.path, node]));
  for (const node of incoming) {
    const existing = byPath.get(node.path);
    byPath.set(node.path, existing ? { ...node, ...existing } : node);
  }
  return sortTreeLevel([...byPath.values()]);
}

function includeSelectedPathChild(
  nodes: TreeNode[],
  parentPath: string,
  selectedPath: string | null
): TreeNode[] {
  if (!selectedPath) return nodes;

  const parentPrefix = parentPath ? `${parentPath}/` : "";
  if (!selectedPath.startsWith(parentPrefix)) return nodes;
  const remainder = selectedPath.slice(parentPrefix.length);
  if (!remainder) return nodes;

  const [name, ...descendants] = remainder.split("/");
  const childPath = parentPrefix ? `${parentPrefix}${name}` : name;
  if (nodes.some((node) => node.path === childPath)) return nodes;

  return sortTreeLevel([
    ...nodes,
    {
      name,
      path: childPath,
      type: descendants.length > 0 ? "dir" : "file",
    },
  ]);
}

function findNodeByPath(nodes: TreeNode[], path: string): TreeNode | null {
  for (const node of nodes) {
    if (node.path === path) {
      return node;
    }
    if (node.type === "dir" && node.children) {
      const found = findNodeByPath(node.children, path);
      if (found) return found;
    }
  }
  return null;
}

function updateFileContent(
  nodes: TreeNode[],
  path: string,
  content: string
): TreeNode[] {
  let changed = false;
  const updated = nodes.map((node) => {
    if (node.type === "file" && node.path === path) {
      changed = true;
      return { ...node, content };
    }
    if (node.children) {
      const children = updateFileContent(node.children, path, content);
      if (children !== node.children) {
        changed = true;
        return { ...node, children };
      }
    }
    return node;
  });
  return changed ? updated : nodes;
}

function listedFilePreview(file: TreeNode): FilePreview | null {
  const size = file.size ?? null;
  if (isBinaryRendererFile(file.name)) {
    return file.url ? { kind: "binary", url: file.url, size } : null;
  }
  return file.content === undefined
    ? null
    : { kind: "text", content: file.content, isTruncated: false, size };
}

/**
 * Find a file node whose path ends with the given suffix.
 * If the suffix matches a directory instead, returns the first file inside it.
 * Useful when S3 paths are prefixed with a trial-name directory.
 */
function findNodeBySuffix(nodes: TreeNode[], suffix: string): TreeNode | null {
  for (const node of nodes) {
    if (node.path === suffix || node.path.endsWith(`/${suffix}`)) {
      if (node.type === "file") return node;
      if (node.type === "dir" && node.children) {
        return findFirstFile(node.children);
      }
    }
    if (node.type === "dir" && node.children) {
      const found = findNodeBySuffix(node.children, suffix);
      if (found) return found;
    }
  }
  return null;
}

/**
 * Find the first file in the tree.
 */
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

function getAncestorPaths(path: string): string[] {
  const parts = path.split("/").filter(Boolean);
  const ancestors: string[] = [];
  let currentPath = "";

  for (let i = 0; i < parts.length - 1; i++) {
    currentPath = currentPath ? `${currentPath}/${parts[i]}` : parts[i];
    ancestors.push(currentPath);
  }

  return ancestors;
}

/**
 * Get the appropriate icon for a file based on its extension.
 */
function getFileIcon(name: string) {
  const ext = name.split(".").pop()?.toLowerCase();
  switch (ext) {
    case "md":
    case "txt":
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

// Language detection is handled by getLanguageFromFilename from code-block

export function TaskFilesPanel({
  isOpen,
  onClose,
  taskId,
  task: taskSnapshot,
  orderedTasks,
  taskIndex,
  onNavigate,
  onNavigateToFirstTrial,
  apiBaseUrl,
  cancelExperimentId,
  allowRetry = true,
  showAnalysis = true,
  activePane,
  onActivePaneChange,
  onRetryComplete,
  contentOnly = false,
  filesUrl,
  loadFilesLazily = false,
  taskVersion,
  initialFilePath,
  staticChecksTaskId,
  taskDetail,
  onOpenTrial,
  overviewTrialsLoading,
  selectedLines,
  onSelectLinesChange,
  onSelectedFileChange,
}: TaskFilesPanelProps) {
  const baseUrl = apiBaseUrl ?? "/api";
  // The TASK OVERVIEW entry is keyed off the task even in filesUrl-driven
  // panes (which pass taskId={null}); staticChecksTaskId supplies the id there.
  const effectiveChecksTaskId = taskId ?? staticChecksTaskId ?? null;
  // The pre_trial_* fields live on the version summaries of /detail, not on
  // the plain task endpoint. Task cards seed this key from their browse rows;
  // SWR replaces that snapshot with the full response when this pane mounts.
  const checksKey =
    effectiveChecksTaskId && showAnalysis !== false
      ? taskDetailKey(effectiveChecksTaskId, baseUrl)
      : null;
  const {
    data: checksResource,
    error: checksLoadError,
    mutate: mutateChecks,
  } = useSWR<TaskDetailResource>(checksKey, fetcher, {
    fallbackData: taskDetail ?? undefined,
    revalidateOnMount: taskDetail == null,
    // Poll quickly while checks or task QA run. Keep a slower poll while the
    // panel is open even after both are terminal: a CLI in-place overwrite can
    // replace this version without changing its number, and the refreshed
    // content hash is what invalidates the file listing and preview caches.
    refreshInterval: (data) => {
      const detail = taskDetailValue(data);
      const checksLive =
        pickChecksVersion(detail, taskVersion)?.pre_trial_status ===
          "running" ||
        pickChecksVersion(detail, taskVersion)?.pre_trial_status === "queued";
      if (checksLive || taskHasActiveVerdict(detail?.task)) return 5000;
      return isOpen ? 30000 : 0;
    },
  });
  const checksDetail = taskDetailValue(checksResource);
  const task = cancelExperimentId
    ? taskSnapshot
    : (checksDetail?.task ?? taskSnapshot);
  const actionsReady =
    checksResource !== undefined && !isBrowseTaskDetail(checksResource);
  // Scoped panes (the experiment drawer) pin the version whose files are on
  // screen; the checks must describe that same source.
  const checksVersion = pickChecksVersion(checksDetail, taskVersion);
  // The pinned version wins outright — falling back to the /detail-resolved
  // version while it loads would briefly widen the trial aggregation to every
  // version. Without a pin, undefined keeps the aggregation waiting until the
  // version resolves; only a loaded task with no versions is genuinely
  // unscoped.
  const overviewVersion =
    taskVersion !== undefined
      ? taskVersion
      : checksVersion
        ? checksVersion.version
        : checksDetail !== undefined
          ? null
          : undefined;
  const overviewAvailable =
    effectiveChecksTaskId !== null && showAnalysis !== false;
  const taskPaneExists = overviewAvailable;
  // Until /detail answers, the checks state is unknown, not "unaudited":
  // an enabled Run button on the misread queues an audit that wipes findings.
  // Never on public shares: `checksKey` is null there, so /detail is not
  // fetched and this would otherwise latch on "loading" forever.
  const checksLoading =
    overviewAvailable &&
    (isBrowseTaskDetail(checksResource) ||
      (checksDetail === undefined && !checksLoadError));
  // A failed revalidation with data already in hand is not "unavailable":
  // SWR keeps the stale data, and hiding live findings behind an error flash
  // on one bad poll is worse than showing them.
  const checksLoadFailure =
    checksLoadError && checksDetail === undefined
      ? "Unable to load the static checks state."
      : null;
  const checksFindings = checksVersion?.pre_trial_findings ?? [];
  const taskQaActive = taskHasActiveVerdict(checksDetail?.task);
  const resolvedFilesUrl = filesUrl ?? `${baseUrl}/tasks/${taskId}/files`;
  // Trial file routes stream the file itself; task file routes answer with a
  // JSON envelope ({path, content, key}, or {url} when presigning). Read that
  // off the route, not off whether a filesUrl prop was passed — the drawer's
  // side-by-side task pane passes a TASK filesUrl, and treating its envelope
  // as the file body rendered every task file blank.
  const fileRouteServesBytes = !/\/tasks\/[^/]+\/files$/.test(resolvedFilesUrl);
  // Task drawers that already defer file bodies also page the tree by
  // directory. Trial files and eager file-only panes keep their existing
  // recursive contract until their callers opt in.
  const loadsTaskTreeByDirectory = loadFilesLazily && !fileRouteServesBytes;
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isRerunning, setIsRerunning] = useState(false);
  const [rerunError, setRerunError] = useState<string | null>(null);
  const [isCancelling, setIsCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);
  // Trajectory analysis is a single task-level QA job (classify every trial,
  // then synthesize the verdict), surfaced as one Run QA action.
  const [isRunningQA, setIsRunningQA] = useState(false);
  const [qaActionError, setQAActionError] = useState<string | null>(null);
  const [fileTree, setFileTree] = useState<TreeNode[]>([]);
  const [directoryListings, setDirectoryListings] = useState<
    Record<string, DirectoryListing>
  >({});
  const [expandedDirs, setExpandedDirs] = useState<Set<string>>(new Set());
  const [selectedFilePath, setSelectedFilePath] = useState<string | null>(null);
  const selectFilePath = useCallback(
    (path: string) => {
      setSelectedFilePath(path);
      onSelectedFileChange?.(path);
    },
    [onSelectedFileChange]
  );
  const [loadingFullFile, setLoadingFullFile] = useState(false);
  const [viewMode, setViewMode] = useState<"rendered" | "raw">("rendered");
  const [copiedTaskName, setCopiedTaskName] = useState(false);
  const [copiedFileContent, setCopiedFileContent] = useState(false);
  const contentRef = useRef<HTMLDivElement>(null);
  const copiedTaskNameTimeoutRef = useRef<number | null>(null);
  const copiedFileContentTimeoutRef = useRef<number | null>(null);
  const listingGenerationRef = useRef(0);
  const activeDirectoryRequestsRef = useRef<Set<string>>(new Set());
  const verdictTaskKey =
    isOpen && taskId ? `${baseUrl}/tasks/${taskId}?include_trials=false` : null;
  const { data: verdictTask } = useSWR<Task>(verdictTaskKey, fetcher, {
    refreshInterval: (data) => {
      if (!data) return 10000;
      const done = data.status === "completed" || data.status === "failed";
      return done ? 0 : 15000;
    },
    revalidateOnFocus: false,
  });
  const currentVersion =
    taskVersion !== undefined
      ? taskVersion
      : ((verdictTask ?? task)?.current_version ?? null);
  const currentContentHash = checksVersion?.content_hash ?? null;
  const shouldScopeFilesToVersion = taskVersion !== undefined || !filesUrl;
  const rootListing = directoryListings[""];
  const visibleTree = useMemo(
    () =>
      loadsTaskTreeByDirectory
        ? includeSelectedPathChild(
            rootListing?.nodes ?? [],
            "",
            selectedFilePath
          )
        : fileTree,
    [fileTree, loadsTaskTreeByDirectory, rootListing?.nodes, selectedFilePath]
  );
  const pagedTaskWrapperCandidate =
    !fileRouteServesBytes &&
    loadsTaskTreeByDirectory &&
    rootListing?.status === "ready" &&
    rootListing.cursor === null &&
    rootListing.nodes.length === 1 &&
    rootListing.nodes[0].type === "dir" &&
    taskFileSectionIdForRootNode(rootListing.nodes[0]) === "other"
      ? rootListing.nodes[0]
      : null;
  const pagedTaskWrapperListing = pagedTaskWrapperCandidate
    ? directoryListings[pagedTaskWrapperCandidate.path]
    : undefined;
  const pagedTaskWrapper =
    pagedTaskWrapperCandidate &&
    pagedTaskWrapperListing?.nodes.some(
      (node) => taskFileSectionIdForRootNode(node) !== "other"
    )
      ? pagedTaskWrapperCandidate
      : null;
  const taskSectionRootPath = pagedTaskWrapper?.path ?? "";
  const taskSectionRootListing = pagedTaskWrapper
    ? directoryListings[pagedTaskWrapper.path]
    : rootListing;
  const taskSectionRootNodes = pagedTaskWrapper
    ? includeSelectedPathChild(
        taskSectionRootListing?.nodes ?? [],
        pagedTaskWrapper.path,
        selectedFilePath
      )
    : visibleTree;
  const taskFileSections = useMemo(
    () =>
      fileRouteServesBytes ? [] : buildTaskFileSections(taskSectionRootNodes),
    [fileRouteServesBytes, taskSectionRootNodes]
  );
  const taskSectionDirectoryPaths = useMemo(
    () =>
      taskFileSections.flatMap((section) =>
        section.items.flatMap((item) =>
          item.kind === "directory" ? [item.node.path] : []
        )
      ),
    [taskFileSections]
  );
  const listedSelectedFile = selectedFilePath
    ? loadsTaskTreeByDirectory
      ? Object.values(directoryListings)
          .flatMap((listing) => listing.nodes)
          .find((node) => node.path === selectedFilePath)
      : findNodeByPath(fileTree, selectedFilePath)
    : null;
  const selectedFile = selectedFilePath
    ? (listedSelectedFile ?? {
        name: getNodeName(selectedFilePath),
        path: selectedFilePath,
        type: "file" as const,
      })
    : null;

  const buildSelectedFileUrl = (presign = false, maxBytes?: number) => {
    if (!selectedFile) return null;
    const params = new URLSearchParams();
    if (presign) params.set("presign", "1");
    if (maxBytes) params.set("max_bytes", String(maxBytes));
    if (shouldScopeFilesToVersion && currentVersion != null) {
      params.set("version", String(currentVersion));
    }
    if (currentContentHash) params.set("source_hash", currentContentHash);
    const query = params.toString();
    return `${resolvedFilesUrl}/${encodeURIComponent(selectedFile.path)}${
      query ? `?${query}` : ""
    }`;
  };

  const listedPreview = selectedFile ? listedFilePreview(selectedFile) : null;
  const directBinaryPreview =
    selectedFile &&
    isBinaryRendererFile(selectedFile.name) &&
    !loadFilesLazily &&
    // Without a presigned URL from the listing, only a byte-serving route can
    // back an <img>/<embed> src directly; a task route would hand it JSON.
    (selectedFile.url || fileRouteServesBytes)
      ? {
          kind: "binary" as const,
          url: selectedFile.url ?? buildSelectedFileUrl()!,
          size: selectedFile.size ?? null,
        }
      : null;
  const immediatePreview = listedPreview ?? directBinaryPreview;
  const previewRequestKey =
    selectedFile && !immediatePreview
      ? [
          "task-file-preview",
          resolvedFilesUrl,
          selectedFile.path,
          shouldScopeFilesToVersion ? currentVersion : null,
          currentContentHash,
          loadFilesLazily,
          fileRouteServesBytes ? "raw" : "json",
          selectedFile.url ?? null,
          selectedFile.size ?? null,
        ]
      : null;
  const {
    data: fetchedPreview,
    error: previewError,
    mutate: mutateFilePreview,
  } = useSWR<FilePreview>(
    previewRequestKey,
    async () => {
      if (!selectedFile) throw new Error("No file selected");
      let size = selectedFile.size ?? null;

      if (isBinaryRendererFile(selectedFile.name)) {
        const url = buildSelectedFileUrl(true);
        if (!url) throw new Error("File URL unavailable");
        const res = await fetch(url);
        if (!res.ok) throw new Error("Failed to fetch file URL");
        const data = (await res.json()) as { url?: string };
        if (!data.url) throw new Error("File URL unavailable");
        return { kind: "binary", url: data.url, size };
      }

      const shouldTruncate =
        selectedFile.size !== undefined &&
        selectedFile.size > TRUNCATE_THRESHOLD;
      let content: string | null = null;
      let isTruncated = false;

      if (selectedFile.url) {
        try {
          const headers: HeadersInit = shouldTruncate
            ? { Range: `bytes=0-${TRUNCATE_THRESHOLD - 1}` }
            : {};
          const s3Res = await fetch(selectedFile.url, { headers });
          if (s3Res.ok || s3Res.status === 206) {
            content = await s3Res.text();
            isTruncated =
              s3Res.status === 206 ||
              (shouldTruncate && content.length >= TRUNCATE_THRESHOLD);
          }
        } catch {
          content = null;
        }
      }

      if (content === null) {
        const url = buildSelectedFileUrl(
          false,
          loadFilesLazily ? TRUNCATE_THRESHOLD : undefined
        );
        if (!url) throw new Error("File content unavailable");
        const res = await fetch(url);
        if (!res.ok) throw new Error("Failed to fetch file content");
        if (fileRouteServesBytes) {
          content = await res.text();
        } else {
          const data = (await res.json()) as {
            content?: string;
            is_truncated?: boolean;
            size?: number;
          };
          content = data.content ?? "";
          isTruncated = data.is_truncated ?? isTruncated;
          size = data.size ?? size;
        }
      }

      return { kind: "text", content, isTruncated, size };
    },
    { revalidateOnFocus: false, shouldRetryOnError: false }
  );
  const selectedPreview = immediatePreview ?? fetchedPreview ?? null;

  const verdictSource = verdictTask ?? task;
  // Task drawers request one directory page at a time. File-only and trial
  // panes retain the recursive/streaming contract because those callers may
  // depend on eager bodies and automatic first-file selection.
  const buildListingUrl = useCallback(
    (prefix?: string, cursor?: string) => {
      const params = new URLSearchParams();
      params.set("recursive", loadsTaskTreeByDirectory ? "0" : "1");
      if (loadFilesLazily) {
        params.set("inline", "0");
        params.set("presign", "0");
      }
      if (!loadsTaskTreeByDirectory && !taskPaneExists && !loadFilesLazily) {
        params.set("stream", "1");
      }
      if (loadsTaskTreeByDirectory) {
        params.set("limit", "100");
        if (prefix) params.set("prefix", prefix);
        if (cursor) params.set("cursor", cursor);
      }
      if (shouldScopeFilesToVersion && currentVersion != null) {
        params.set("version", String(currentVersion));
      }
      if (currentContentHash) params.set("source_hash", currentContentHash);
      return `${resolvedFilesUrl}?${params.toString()}`;
    },
    [
      resolvedFilesUrl,
      shouldScopeFilesToVersion,
      currentVersion,
      currentContentHash,
      taskPaneExists,
      loadFilesLazily,
      loadsTaskTreeByDirectory,
    ]
  );

  const orderedList = useMemo(() => orderedTasks ?? [], [orderedTasks]);
  const resolvedIndex =
    typeof taskIndex === "number" && taskIndex >= 0
      ? taskIndex
      : orderedList.findIndex((item) => item.id === taskId);
  const hasNavigation =
    Boolean(onNavigate) && orderedList.length > 1 && resolvedIndex >= 0;
  const canGoPrev = hasNavigation && resolvedIndex > 0;
  const canGoNext = hasNavigation && resolvedIndex < orderedList.length - 1;

  const retryableTrials = useMemo(() => {
    if (!task?.trials) return [];
    // Agent trials only: task.trials now carries qa/audit rows too, and
    // "Rerun trials" must never replay an analysis brief through the
    // generic retry endpoint (it also refuses them server-side).
    return task.trials.filter(
      (trial) =>
        isAgentTrial(trial) &&
        (trial.status === "failed" || trial.status === "success")
    );
  }, [task]);

  const canRetryTask = actionsReady && allowRetry && retryableTrials.length > 0;
  const canCancelTask =
    actionsReady && allowRetry && taskHasCancellableWork(task);
  const cancelActionLabel = getCancelActionLabel(task);
  const allTrialsTerminal =
    Boolean(task?.trials?.length) &&
    (task?.trials ?? []).every(
      (trial) =>
        trial.status === "failed" ||
        trial.status === "success" ||
        trial.status === "skipped"
    );
  const hasAnalysisInFlight = (task?.trials ?? []).some((trial) =>
    isActivePipelineStatus(trial.analysis_status)
  );
  const verdictInFlight = isActivePipelineStatus(verdictSource?.verdict_status);
  const canRunQA =
    actionsReady &&
    allowRetry &&
    Boolean(task) &&
    allTrialsTerminal &&
    !hasAnalysisInFlight &&
    !verdictInFlight;
  const qaActionLabel =
    verdictSource?.verdict_status ||
    verdictSource?.verdict ||
    (task?.trials ?? []).some(
      (trial) => trial.analysis_status || trial.analysis
    )
      ? "Rerun QA"
      : "Run QA";

  const navigateTo = useCallback(
    (nextIndex: number) => {
      if (!onNavigate) return;
      const nextTask = orderedList[nextIndex];
      if (!nextTask) return;
      onNavigate(nextTask, nextIndex);
    },
    [onNavigate, orderedList]
  );

  const handleRetryTask = async () => {
    if (!canRetryTask || isRerunning) return;
    setIsRerunning(true);
    setRerunError(null);

    try {
      const results = await Promise.allSettled(
        retryableTrials.map(async (trial: Trial) => {
          const res = await fetch(`${baseUrl}/trials/${trial.id}/retry`, {
            method: "POST",
          });
          if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            throw new Error(
              data.detail || data.error || "Failed to retry trial"
            );
          }
        })
      );
      const failures = results.filter((result) => result.status === "rejected");
      if (failures.length > 0) {
        setRerunError(`Failed to rerun ${failures.length} trial(s).`);
      } else {
        setRerunError(null);
      }
      onRetryComplete?.(task?.id ? [task.id] : taskId ? [taskId] : undefined);
    } finally {
      setIsRerunning(false);
    }
  };

  const handleCancelTask = async () => {
    if (!canCancelTask || isCancelling) return;
    setIsCancelling(true);
    setCancelError(null);

    try {
      const id = task?.id ?? taskId;
      let path = `${baseUrl}/tasks/cancel`;
      let body: string | undefined = JSON.stringify({
        task_ids: id ? [id] : [],
        ...(cancelExperimentId ? { experiment_id: cancelExperimentId } : {}),
      });
      // No active trials but analysis in flight (QA or the source audit --
      // qa/cancel covers both kinds) -> cancel just the task QA job.
      // Experiment-scoped cancel leaves shared QA alone unless the caller is
      // on the dedicated cancel-QA path.
      if (
        id &&
        !cancelExperimentId &&
        !taskHasActiveTrials(task) &&
        (taskHasActiveVerdict(task) ||
          taskHasActiveAnalysis(task) ||
          taskHasLiveAnalysisTrial(task))
      ) {
        path = `${baseUrl}/tasks/${id}/qa/cancel`;
        body = undefined;
      }
      const res = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || data.error || "Failed to cancel task");
      }
      setCancelError(null);
      onRetryComplete?.(id ? [id] : undefined);
    } catch (err) {
      setCancelError(
        err instanceof Error ? err.message : "Failed to cancel task"
      );
    } finally {
      setIsCancelling(false);
    }
  };

  const handleRunQA = async () => {
    if (!task?.id || !canRunQA || isRunningQA) return;
    setIsRunningQA(true);
    setQAActionError(null);

    try {
      // One task-level QA job: (re)classify every trial and then synthesize
      // the task verdict.
      const res = await fetch(`${baseUrl}/tasks/${task.id}/qa/retry`, {
        method: "POST",
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || data.error || "Failed to queue task QA");
      }
      onRetryComplete?.([task.id]);
      // The QA-active guard reads this cache; refresh it so the guard flips
      // on now instead of after the next unrelated revalidation.
      void mutateChecks();
    } catch (err) {
      setQAActionError(
        err instanceof Error ? err.message : "Failed to queue task QA"
      );
    } finally {
      setIsRunningQA(false);
    }
  };

  useEffect(() => {
    setRerunError(null);
    setIsRerunning(false);
    setQAActionError(null);
    setIsRunningQA(false);
  }, [taskId]);

  const isEditableTarget = (target: EventTarget | null) => {
    if (!target || !(target instanceof HTMLElement)) return false;
    const tag = target.tagName.toLowerCase();
    return (
      tag === "input" ||
      tag === "textarea" ||
      target.isContentEditable ||
      target.getAttribute("role") === "textbox"
    );
  };

  const [checksRerunning, setChecksRerunning] = useState(false);
  const [checksQueueError, setChecksQueueError] = useState<string | null>(null);
  // Another task's failed queue attempt is not this task's error.
  useEffect(() => {
    setChecksQueueError(null);
    setChecksRerunning(false);
  }, [effectiveChecksTaskId]);
  const handleRerunChecks = useCallback(async () => {
    if (!effectiveChecksTaskId || checksRerunning) return;
    setChecksRerunning(true);
    setChecksQueueError(null);
    try {
      const res = await fetch(
        `${baseUrl}/tasks/${effectiveChecksTaskId}/qa/pre-trial`,
        { method: "POST" }
      );
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(
          data.detail || data.error || "Failed to queue static checks"
        );
      }
      await mutateChecks();
    } catch (e) {
      setChecksQueueError(e instanceof Error ? e.message : String(e));
    } finally {
      setChecksRerunning(false);
    }
  }, [baseUrl, effectiveChecksTaskId, checksRerunning, mutateChecks]);

  const loadDirectoryPage = useCallback(
    async (path: string | null, cursor?: string | null) => {
      if (!loadsTaskTreeByDirectory) return;

      const directoryKey = path ?? "";
      const generation = listingGenerationRef.current;
      const requestKey = `${generation}:${directoryKey}`;
      if (activeDirectoryRequestsRef.current.has(requestKey)) return;
      activeDirectoryRequestsRef.current.add(requestKey);
      setDirectoryListings((listings) => ({
        ...listings,
        [directoryKey]: {
          nodes: listings[directoryKey]?.nodes ?? [],
          cursor: listings[directoryKey]?.cursor ?? null,
          status: "loading",
        },
      }));

      try {
        const res = await fetch(
          buildListingUrl(path ?? undefined, cursor ?? undefined)
        );
        if (!res.ok) throw new Error("Failed to fetch files");
        const data = (await res.json()) as FilesListingResponse;
        if (listingGenerationRef.current !== generation) return;

        const page = buildDirectoryPage(data);
        setDirectoryListings((listings) => ({
          ...listings,
          [directoryKey]: {
            nodes: mergeTreeLevel(listings[directoryKey]?.nodes ?? [], page),
            cursor: data.cursor ?? null,
            status: "ready",
          },
        }));
      } catch {
        if (listingGenerationRef.current === generation) {
          setDirectoryListings((listings) => ({
            ...listings,
            [directoryKey]: {
              nodes: listings[directoryKey]?.nodes ?? [],
              cursor: listings[directoryKey]?.cursor ?? null,
              status: "error",
            },
          }));
        }
      } finally {
        activeDirectoryRequestsRef.current.delete(requestKey);
      }
    },
    [buildListingUrl, loadsTaskTreeByDirectory]
  );

  // Conventional task directories are presented as section contents rather
  // than folder rows. Fetch only those directory pages, using the same bounded
  // listing path and cursor state as an explicitly expanded folder. A neutral
  // one-directory archive wrapper is resolved first, then its task-root
  // children determine which semantic directories need pages.
  useEffect(() => {
    if (
      !isOpen ||
      activePane !== "file" ||
      !loadsTaskTreeByDirectory ||
      fileRouteServesBytes
    ) {
      return;
    }

    const directoryPaths =
      pagedTaskWrapperCandidate && !pagedTaskWrapperListing
        ? [pagedTaskWrapperCandidate.path]
        : taskSectionDirectoryPaths;
    for (const directoryPath of directoryPaths) {
      if (!directoryListings[directoryPath]) {
        void loadDirectoryPage(directoryPath);
      }
    }
  }, [
    activePane,
    directoryListings,
    fileRouteServesBytes,
    isOpen,
    loadDirectoryPage,
    loadsTaskTreeByDirectory,
    pagedTaskWrapperCandidate,
    pagedTaskWrapperListing,
    taskSectionDirectoryPaths,
  ]);

  // Fetch root file list when panel opens
  useEffect(() => {
    if (!isOpen || activePane !== "file" || (!taskId && !filesUrl)) {
      return;
    }

    let cancelled = false;
    const controller = new AbortController();
    const generation = listingGenerationRef.current + 1;
    listingGenerationRef.current = generation;

    async function fetchFiles() {
      setLoading(true);
      setError(null);
      setFileTree([]);
      setDirectoryListings({});
      setSelectedFilePath(null);
      setExpandedDirs(new Set());

      // Once the tree is painted, later stream failures must not replace
      // a usable tree with an error state — missing bodies just fall back
      // to per-file fetches on click.
      let paintedTree = false;

      // The overview pane is the default view, so nothing pre-selects
      // behind it: a hidden auto-selected file prefetches content that
      // later flashes under whichever file the user actually picks. Only
      // the file-only view (public share) paints a file immediately.
      // Prefer instruction.md — the tree is fully nested, so a plain
      // first-file walk would land inside environment/ instead.
      const applyListing = (tree: TreeNode[]) => {
        paintedTree = true;
        setFileTree(tree);
      };

      try {
        const res = await fetch(buildListingUrl(), {
          signal: controller.signal,
        });
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          throw new Error(
            data.detail || `Failed to fetch files: ${res.statusText}`
          );
        }

        const contentType = res.headers.get("content-type") ?? "";
        if (contentType.includes("application/x-ndjson") && res.body) {
          // Streamed listing: the tree paints as soon as the first chunk
          // lands; file bodies keep trickling in behind it.
          let receivedListing = false;
          for await (const raw of iterateNdjsonLines(res.body)) {
            if (cancelled) return;
            const chunk = raw as FilesStreamChunk;
            if (chunk.type === "listing" && !receivedListing) {
              const tree = buildTreeFromListing(chunk.files || []);
              receivedListing = true;
              applyListing(tree);
              setLoading(false);
            } else if (chunk.type === "content" && receivedListing) {
              setFileTree((tree) =>
                updateFileContent(tree, chunk.path, chunk.content)
              );
            }
          }
          if (!receivedListing) {
            throw new Error("Failed to fetch files");
          }
        } else {
          // Plain JSON listing (trial files, and any non-streaming source).
          const data: FilesListingResponse = await res.json();
          if (cancelled) return;
          if (loadsTaskTreeByDirectory) {
            paintedTree = true;
            setDirectoryListings((listings) => ({
              ...listings,
              "": {
                nodes: buildDirectoryPage(data),
                cursor: data.cursor ?? null,
                status: "ready",
              },
            }));
          } else {
            applyListing(buildTreeFromListing(data.files || []));
          }
        }
      } catch (err) {
        if (!cancelled && !paintedTree) {
          setError(
            err instanceof Error ? err.message : "Failed to fetch files"
          );
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    fetchFiles();

    return () => {
      cancelled = true;
      controller.abort();
      if (listingGenerationRef.current === generation) {
        listingGenerationRef.current += 1;
      }
    };
  }, [
    isOpen,
    activePane,
    taskId,
    filesUrl,
    resolvedFilesUrl,
    buildListingUrl,
    taskPaneExists,
    loadsTaskTreeByDirectory,
  ]);

  // Paint a default file in file-only panes such as public task shares and
  // trial files.
  //
  // A deep-linked initialFilePath owns the first selection: letting the
  // default land first would report the wrong path upward and clear the
  // link's line anchor before the target file is applied.
  useEffect(() => {
    if (!isOpen || activePane !== "file" || taskPaneExists) return;
    if (initialFilePath || selectedFilePath) return;
    if (!visibleTree.length) return;

    if (loadsTaskTreeByDirectory) {
      const directoryPath = [...expandedDirs].sort(
        (left, right) => right.split("/").length - left.split("/").length
      )[0];
      const listing = directoryListings[directoryPath ?? ""];
      if (!listing || listing.status === "loading") return;

      const defaultFile =
        findNodeBySuffix(listing.nodes, "instruction.md") ??
        listing.nodes.find((node) => node.type === "file");
      if (defaultFile) {
        selectFilePath(defaultFile.path);
        return;
      }

      const firstDirectory = listing.nodes.find((node) => node.type === "dir");
      if (!firstDirectory) return;
      setExpandedDirs((current) => {
        if (current.has(firstDirectory.path)) return current;
        return new Set(current).add(firstDirectory.path);
      });
      if (!directoryListings[firstDirectory.path]) {
        void loadDirectoryPage(firstDirectory.path);
      }
      return;
    }

    const defaultFile =
      findNodeBySuffix(visibleTree, "instruction.md") ??
      visibleTree.find((node) => node.type === "file") ??
      findFirstFile(visibleTree);
    if (defaultFile) selectFilePath(defaultFile.path);
  }, [
    activePane,
    directoryListings,
    expandedDirs,
    initialFilePath,
    isOpen,
    loadDirectoryPage,
    loadsTaskTreeByDirectory,
    selectFilePath,
    selectedFilePath,
    taskPaneExists,
    visibleTree,
  ]);

  // Load full file content (when user clicks "Load full file")
  async function loadFullFile() {
    if (!selectedFile) return;

    setLoadingFullFile(true);
    try {
      if (selectedFile.url) {
        const s3Res = await fetch(selectedFile.url);
        if (s3Res.ok) {
          const content = await s3Res.text();
          await mutateFilePreview(
            {
              kind: "text",
              content,
              isTruncated: false,
              size: selectedFile.size ?? null,
            },
            { revalidate: false }
          );
        }
        return;
      }

      const url = buildSelectedFileUrl();
      if (!url) return;
      const res = await fetch(url);
      if (!res.ok) {
        return;
      }
      let content: string;
      if (fileRouteServesBytes) {
        content = await res.text();
      } else {
        const data = (await res.json()) as { content?: string };
        content = data.content ?? "";
      }
      await mutateFilePreview(
        {
          kind: "text",
          content,
          isTruncated: false,
          size: selectedFile.size ?? null,
        },
        { revalidate: false }
      );
    } catch {
      // Keep truncated content on error
    } finally {
      setLoadingFullFile(false);
    }
  }

  // Scroll to top when selected file changes
  useEffect(() => {
    if (contentRef.current) {
      contentRef.current.scrollTop = 0;
    }
  }, [selectedFilePath]);

  // Reset state when panel closes or task changes
  useEffect(() => {
    if (!isOpen) {
      setFileTree([]);
      setDirectoryListings({});
      setSelectedFilePath(null);
      setError(null);
      setExpandedDirs(new Set());
      setLoadingFullFile(false);
      setQAActionError(null);
      setIsRunningQA(false);
    }
  }, [isOpen, taskId]);

  // Synchronize a deep-linked file with its selection and directory pages.
  useEffect(() => {
    if (!isOpen || activePane !== "file" || !initialFilePath) return;
    if (!loadsTaskTreeByDirectory && fileTree.length === 0) return;

    const node = loadsTaskTreeByDirectory
      ? null
      : (findNodeByPath(fileTree, initialFilePath) ??
        findNodeBySuffix(fileTree, initialFilePath));
    const targetPath = node?.path ?? initialFilePath;
    const ancestorPaths = getAncestorPaths(targetPath);
    if (ancestorPaths.length > 0) {
      setExpandedDirs((prev) => {
        if (ancestorPaths.every((path) => prev.has(path))) return prev;
        const next = new Set(prev);
        for (const ancestorPath of ancestorPaths) {
          next.add(ancestorPath);
        }
        return next;
      });
    }

    if (loadsTaskTreeByDirectory) {
      for (const ancestorPath of ancestorPaths) {
        if (!directoryListings[ancestorPath]) {
          void loadDirectoryPage(ancestorPath);
        }
      }
    }

    if (node?.type === "dir") return;

    // A file URL is already an exact resource address. Selecting it does not
    // depend on whether its containing directory page happens to include it.
    if (selectedFilePath !== targetPath) selectFilePath(targetPath);
  }, [
    activePane,
    directoryListings,
    fileTree,
    initialFilePath,
    isOpen,
    loadDirectoryPage,
    loadsTaskTreeByDirectory,
    selectFilePath,
    selectedFilePath,
  ]);

  useEffect(() => {
    if (!isOpen) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (isEditableTarget(event.target)) return;

      // Horizontal navigation (left/right) - between task and trials
      if (event.key === "ArrowRight" && onNavigateToFirstTrial) {
        event.preventDefault();
        onNavigateToFirstTrial();
      }
      // ArrowLeft does nothing in task view (task is the first item)

      // Vertical navigation (up/down) - between tasks in list
      if (hasNavigation) {
        if (event.key === "ArrowUp" && canGoPrev) {
          event.preventDefault();
          navigateTo(resolvedIndex - 1);
        } else if (event.key === "ArrowDown" && canGoNext) {
          event.preventDefault();
          navigateTo(resolvedIndex + 1);
        }
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [
    isOpen,
    hasNavigation,
    canGoPrev,
    canGoNext,
    resolvedIndex,
    navigateTo,
    onNavigateToFirstTrial,
  ]);

  function toggleDir(node: TreeNode) {
    const isExpanded = expandedDirs.has(node.path);
    setExpandedDirs((prev) => {
      const next = new Set(prev);
      if (isExpanded) {
        next.delete(node.path);
      } else {
        next.add(node.path);
      }
      return next;
    });
    if (
      !isExpanded &&
      loadsTaskTreeByDirectory &&
      !directoryListings[node.path]
    ) {
      void loadDirectoryPage(node.path);
    }
  }

  function renderDirectoryContents(node: TreeNode, depth: number) {
    const directory = loadsTaskTreeByDirectory
      ? directoryListings[node.path]
      : undefined;
    const children = loadsTaskTreeByDirectory
      ? includeSelectedPathChild(
          directory?.nodes ?? [],
          node.path,
          selectedFilePath
        )
      : node.children;

    return (
      <div>
        {children ? renderFileTree(children, depth) : null}
        {loadsTaskTreeByDirectory &&
        (!directory || directory.status === "loading") ? (
          <div
            className="text-muted-foreground flex items-center gap-1.5 py-1 text-xs"
            style={{ paddingLeft: `${depth * 12 + 8}px` }}
          >
            <Loader2 className="h-3 w-3 animate-spin" />
            Loading…
          </div>
        ) : null}
        {directory?.status === "error" ? (
          <button
            type="button"
            className="text-destructive hover:text-destructive/80 py-1 text-left text-xs"
            style={{ paddingLeft: `${depth * 12 + 8}px` }}
            onClick={() => void loadDirectoryPage(node.path, directory.cursor)}
          >
            Unable to load. Retry
          </button>
        ) : null}
        {directory?.cursor && directory.status !== "loading" ? (
          <button
            type="button"
            className="text-muted-foreground hover:text-foreground py-1 text-left text-xs"
            style={{ paddingLeft: `${depth * 12 + 8}px` }}
            onClick={() => void loadDirectoryPage(node.path, directory.cursor)}
          >
            Load more
          </button>
        ) : null}
      </div>
    );
  }

  function renderFileTree(nodes: TreeNode[], depth = 0) {
    return nodes.map((node) => {
      const isExpanded = expandedDirs.has(node.path);
      const isSelected =
        activePane === "file" && selectedFile?.path === node.path;
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
              if (node.type === "dir") {
                toggleDir(node);
              } else {
                selectFilePath(node.path);
                onActivePaneChange?.("file");
              }
            }}
            className={`h-auto w-full justify-start gap-1.5 rounded px-2 py-1 text-left font-mono text-xs transition-colors ${
              isSelected
                ? "bg-primary/20 text-primary hover:bg-primary/20"
                : "text-foreground hover:bg-muted"
            }`}
            style={{ paddingLeft: `${depth * 12 + 8}px` }}
          >
            {node.type === "dir" && (
              <span className="flex h-3 w-3 items-center justify-center">
                {isExpanded ? (
                  <ChevronDown className="text-muted-foreground h-3 w-3" />
                ) : (
                  <ChevronRight className="text-muted-foreground h-3 w-3" />
                )}
              </span>
            )}
            {node.type === "file" && <span className="w-3" />}
            <Icon
              className={`h-4 w-4 shrink-0 ${
                node.type === "dir"
                  ? "text-yellow-500"
                  : "text-muted-foreground"
              }`}
            />
            <span className="min-w-0 flex-1 truncate">{node.name}</span>
            {node.type === "file" && node.size != null ? (
              <span className="text-muted-foreground shrink-0 pl-2 text-[10px] tabular-nums">
                {formatFileSize(node.size)}
              </span>
            ) : null}
          </Button>
          {node.type === "dir" && isExpanded
            ? renderDirectoryContents(node, depth + 1)
            : null}
        </div>
      );
    });
  }

  const renderFileContent = () => {
    if (!selectedFile) {
      return (
        <div className="text-muted-foreground flex h-full items-center justify-center text-sm">
          Select a file to view its contents
        </div>
      );
    }

    if (!selectedPreview && !previewError) {
      return (
        <div className="space-y-2 p-4">
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-3/4" />
          <Skeleton className="h-4 w-5/6" />
        </div>
      );
    }
    if (previewError || !selectedPreview) {
      return (
        <div className="text-muted-foreground flex h-full items-center justify-center text-sm">
          {FILE_LOAD_ERROR}
        </div>
      );
    }

    const isBinary = isBinaryRendererFile(selectedFile.name);

    return (
      <div className="flex h-full flex-col">
        <div className="min-h-0 flex-1 overflow-auto">
          <FileRenderer
            fileName={selectedFile.name}
            url={selectedPreview.kind === "binary" ? selectedPreview.url : null}
            content={
              selectedPreview.kind === "text" ? selectedPreview.content : null
            }
            fileSize={selectedPreview.size ?? selectedFile.size}
            viewMode={viewMode}
            selectedLines={selectedLines}
            onSelectLines={onSelectLinesChange}
          />
        </div>
        {!isBinary &&
          selectedPreview.kind === "text" &&
          selectedPreview.isTruncated && (
            <div className="border-border bg-muted/50 flex items-center justify-between border-t px-4 py-3">
              <span className="text-muted-foreground text-xs">
                Showing first {formatFileSize(TRUNCATE_THRESHOLD)} of{" "}
                {selectedPreview.size
                  ? formatFileSize(selectedPreview.size)
                  : "large file"}
              </span>
              <Button
                type="button"
                size="sm"
                onClick={loadFullFile}
                disabled={loadingFullFile}
                className="h-auto px-3 py-1.5 text-xs"
              >
                {loadingFullFile ? "Loading..." : "Load full file"}
              </Button>
            </div>
          )}
      </div>
    );
  };

  const resolvedTaskId = task?.id ?? taskId ?? "—";
  const taskName = task?.name ?? resolvedTaskId;
  useEffect(() => {
    setCopiedTaskName(false);
    if (copiedTaskNameTimeoutRef.current !== null) {
      window.clearTimeout(copiedTaskNameTimeoutRef.current);
      copiedTaskNameTimeoutRef.current = null;
    }
  }, [taskName]);

  useEffect(() => {
    setCopiedFileContent(false);
    if (copiedFileContentTimeoutRef.current !== null) {
      window.clearTimeout(copiedFileContentTimeoutRef.current);
      copiedFileContentTimeoutRef.current = null;
    }
  }, [selectedFile?.path]);

  useEffect(() => {
    return () => {
      if (copiedTaskNameTimeoutRef.current !== null) {
        window.clearTimeout(copiedTaskNameTimeoutRef.current);
      }
      if (copiedFileContentTimeoutRef.current !== null) {
        window.clearTimeout(copiedFileContentTimeoutRef.current);
      }
    };
  }, []);

  const { rewardSuccess, rewardTotal, averageRewardPct } = useMemo(() => {
    const trials = task?.trials ?? [];
    const versionTrials =
      currentVersion != null
        ? trials.filter((t) => t.task_version === currentVersion)
        : trials;
    const rewardSum = versionTrials.reduce(
      (sum, trial) => sum + (trial.reward ?? 0),
      0
    );
    const total = versionTrials.filter((t) => t.reward != null).length;
    return {
      rewardSuccess: total > 0 ? rewardSum : null,
      rewardTotal: total > 0 ? total : null,
      averageRewardPct:
        total > 0 ? Math.round((rewardSum / total) * 100) : null,
    };
  }, [task?.trials, currentVersion]);

  if (!taskId && !filesUrl) {
    return null;
  }

  const handleCopyTaskName = async () => {
    await navigator.clipboard.writeText(taskName);
    setCopiedTaskName(true);
    if (copiedTaskNameTimeoutRef.current !== null) {
      window.clearTimeout(copiedTaskNameTimeoutRef.current);
    }
    copiedTaskNameTimeoutRef.current = window.setTimeout(() => {
      setCopiedTaskName(false);
      copiedTaskNameTimeoutRef.current = null;
    }, 2000);
  };

  const handleCopyFileContent = async () => {
    if (selectedPreview?.kind !== "text") return;
    await navigator.clipboard.writeText(selectedPreview.content);
    setCopiedFileContent(true);
    if (copiedFileContentTimeoutRef.current !== null) {
      window.clearTimeout(copiedFileContentTimeoutRef.current);
    }
    copiedFileContentTimeoutRef.current = window.setTimeout(() => {
      setCopiedFileContent(false);
      copiedFileContentTimeoutRef.current = null;
    }, 2000);
  };

  const isListingLoading = loading;
  const listingError = error;

  // Whole-pane skeleton mirroring the sidebar + content layout while the
  // single listing request is in flight.
  const listingSkeleton = (
    <div className="flex flex-1 flex-col overflow-hidden md:flex-row">
      <div className="border-border bg-muted/30 max-h-[30vh] w-full border-b p-2 md:max-h-none md:w-56 md:border-r md:border-b-0 lg:w-64">
        <div className="space-y-2 px-2 py-2">
          {taskPaneExists && (
            <>
              <Skeleton className="h-3 w-24" />
              <Skeleton className="h-6 w-full" />
              <div className="pt-2" />
            </>
          )}
          <Skeleton className="h-3 w-12" />
          <Skeleton className="h-6 w-full" />
          <Skeleton className="h-6 w-5/6" />
          <Skeleton className="h-6 w-3/4" />
          <Skeleton className="h-6 w-5/6" />
          <Skeleton className="h-6 w-2/3" />
        </div>
      </div>
      <div className="flex-1 space-y-3 overflow-hidden p-4 sm:p-6">
        <Skeleton className="h-5 w-1/3" />
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-5/6" />
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="h-4 w-3/4" />
        <Skeleton className="h-4 w-5/6" />
      </div>
    </div>
  );

  const fileTreeContent = (
    <div className="@container/file-browser flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
      {isListingLoading ? (
        listingSkeleton
      ) : listingError && !taskPaneExists ? (
        <div className="flex flex-1 items-center justify-center p-4 sm:p-6">
          <div className="space-y-2 text-center">
            <AlertCircle className="mx-auto h-8 w-8 text-red-500" />
            <p className="text-muted-foreground text-sm">
              Unable to load files
            </p>
            <p className="text-muted-foreground text-xs">{listingError}</p>
          </div>
        </div>
      ) : visibleTree.length === 0 && !taskPaneExists ? (
        <div className="flex flex-1 items-center justify-center p-4 sm:p-6">
          <div className="space-y-2 text-center">
            <p className="text-muted-foreground text-sm">No files found</p>
            {!filesUrl && (
              <p className="text-muted-foreground text-xs">
                The task directory may be empty or not uploaded to S3
              </p>
            )}
          </div>
        </div>
      ) : (
        <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden @2xl/file-browser:flex-row">
          <div className="border-border bg-muted/30 max-h-[30vh] w-full overflow-auto border-b @2xl/file-browser:max-h-none @2xl/file-browser:w-56 @2xl/file-browser:shrink-0 @2xl/file-browser:border-r @2xl/file-browser:border-b-0 @3xl/file-browser:w-64">
            <div className="p-2">
              {taskPaneExists && (
                <div className="border-border mb-2 border-b pb-2">
                  <div className="text-muted-foreground px-2 py-2 font-mono text-[10px] font-semibold tracking-wide uppercase sm:text-xs">
                    Task
                  </div>
                  {overviewAvailable ? (
                    <button
                      type="button"
                      onClick={() => onActivePaneChange?.("overview")}
                      aria-current={
                        activePane === "overview" ? "page" : undefined
                      }
                      className={`flex w-full items-center gap-1.5 rounded px-2 py-1 text-left text-sm ${
                        activePane === "overview"
                          ? "bg-primary/20 text-primary"
                          : "hover:bg-muted/50 cursor-pointer"
                      }`}
                      title="View task QA and aggregated trial QA"
                    >
                      <ListChecks
                        className="h-3.5 w-3.5 shrink-0"
                        aria-hidden="true"
                      />
                      <span className="truncate">
                        {checksLoading
                          ? "Loading…"
                          : checksLoadFailure
                            ? "Unavailable"
                            : "Overview"}
                      </span>
                    </button>
                  ) : null}
                  <button
                    type="button"
                    onClick={() => onActivePaneChange?.("file")}
                    aria-current={activePane === "file" ? "page" : undefined}
                    className={`flex w-full items-center gap-1.5 rounded px-2 py-1 text-left text-sm ${
                      activePane === "file"
                        ? "bg-primary/20 text-primary"
                        : "hover:bg-muted/50 cursor-pointer"
                    }`}
                    title="Browse task files"
                  >
                    <FolderOpen
                      className="h-3.5 w-3.5 shrink-0"
                      aria-hidden="true"
                    />
                    <span className="truncate">Files</span>
                  </button>
                </div>
              )}
              {!taskPaneExists ? (
                <div className="text-muted-foreground px-2 py-2 font-mono text-[10px] font-semibold tracking-wide uppercase sm:text-xs">
                  Files
                </div>
              ) : null}
              {listingError ? (
                <p className="text-muted-foreground px-2 py-2 text-xs">
                  Unable to load files: {listingError}
                </p>
              ) : visibleTree.length === 0 ? (
                <p className="text-muted-foreground px-2 py-2 text-xs">
                  No files found
                </p>
              ) : !fileRouteServesBytes ? (
                <>
                  {pagedTaskWrapperCandidate &&
                  !pagedTaskWrapper &&
                  (!pagedTaskWrapperListing ||
                    pagedTaskWrapperListing.status === "loading") ? (
                    <div className="text-muted-foreground flex items-center gap-1.5 px-2 py-1 text-xs">
                      <Loader2 className="h-3 w-3 animate-spin" />
                      Loading task files…
                    </div>
                  ) : (
                    taskFileSections.map((section) => {
                      const SectionIcon = TASK_FILE_SECTION_ICONS[section.id];
                      return (
                        <section key={section.id} className="mb-3 last:mb-0">
                          <h3 className="text-muted-foreground flex items-center gap-2 px-2 py-1.5 font-mono text-[10px] font-semibold tracking-wider uppercase sm:text-xs">
                            <SectionIcon
                              className="h-3.5 w-3.5 shrink-0"
                              aria-hidden="true"
                            />
                            <span>{section.label}</span>
                          </h3>
                          {section.items.map((item) => (
                            <div key={item.node.path}>
                              {item.kind === "directory"
                                ? renderDirectoryContents(item.node, 0)
                                : renderFileTree([item.node])}
                            </div>
                          ))}
                        </section>
                      );
                    })
                  )}
                  {taskSectionRootListing?.cursor ? (
                    <button
                      type="button"
                      className="text-muted-foreground hover:text-foreground flex items-center gap-1.5 px-2 py-1 text-xs"
                      onClick={() =>
                        void loadDirectoryPage(
                          taskSectionRootPath || null,
                          taskSectionRootListing.cursor
                        )
                      }
                      disabled={taskSectionRootListing.status === "loading"}
                    >
                      {taskSectionRootListing.status === "loading" ? (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      ) : null}
                      Load more
                    </button>
                  ) : null}
                  {taskSectionRootListing?.status === "error" ? (
                    <button
                      type="button"
                      className="text-destructive hover:text-destructive/80 px-2 py-1 text-left text-xs"
                      onClick={() =>
                        void loadDirectoryPage(
                          taskSectionRootPath || null,
                          taskSectionRootListing.cursor
                        )
                      }
                    >
                      Unable to load. Retry
                    </button>
                  ) : null}
                </>
              ) : (
                <>
                  {renderFileTree(visibleTree)}
                  {rootListing?.cursor ? (
                    <button
                      type="button"
                      className="text-muted-foreground hover:text-foreground flex items-center gap-1.5 px-2 py-1 text-xs"
                      onClick={() =>
                        void loadDirectoryPage(null, rootListing.cursor)
                      }
                      disabled={rootListing.status === "loading"}
                    >
                      {rootListing.status === "loading" ? (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      ) : null}
                      Load more
                    </button>
                  ) : null}
                  {rootListing?.status === "error" ? (
                    <p className="text-destructive px-2 py-1 text-xs">
                      Unable to load the next page. Retry with Load more.
                    </p>
                  ) : null}
                </>
              )}
            </div>
          </div>
          <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
            {activePane === "file" && selectedFile && (
              <div className="border-border bg-muted/30 flex items-center justify-between gap-2 border-b px-3 py-2 sm:px-4">
                <div className="text-muted-foreground min-w-0 flex-1 truncate font-mono text-[10px] sm:text-xs">
                  {selectedFile.path}
                </div>
                {!isBinaryRendererFile(selectedFile.name) && (
                  <div className="flex shrink-0 items-center gap-2">
                    <Tabs
                      value={viewMode}
                      onValueChange={(v) =>
                        setViewMode(v as "rendered" | "raw")
                      }
                    >
                      <TabsList className="h-7">
                        <TabsTrigger
                          value="rendered"
                          className="h-6 px-2 text-[10px]"
                        >
                          <Eye className="mr-1 h-3 w-3" />
                          Rendered
                        </TabsTrigger>
                        <TabsTrigger
                          value="raw"
                          className="h-6 px-2 text-[10px]"
                        >
                          <Code className="mr-1 h-3 w-3" />
                          Raw
                        </TabsTrigger>
                      </TabsList>
                    </Tabs>
                    <Button
                      type="button"
                      variant="outline"
                      onClick={handleCopyFileContent}
                      disabled={selectedPreview?.kind !== "text"}
                      className="h-auto w-7 self-stretch p-0"
                      title="Copy raw content"
                      aria-label="Copy raw content"
                    >
                      {copiedFileContent ? (
                        <Check className="h-3 w-3" />
                      ) : (
                        <Copy className="h-3 w-3" />
                      )}
                    </Button>
                  </div>
                )}
              </div>
            )}
            <div ref={contentRef} className="bg-card flex-1 overflow-auto">
              {activePane === "overview" && overviewAvailable ? (
                <TaskOverviewPanel
                  taskId={effectiveChecksTaskId}
                  apiBaseUrl={baseUrl}
                  version={overviewVersion}
                  // The host's rows are the authoritative set: an experiment
                  // drawer aggregates only its own trials. A task prop with
                  // no trials yet still scopes (empty + overviewTrialsLoading
                  // renders as loading).
                  scopeTrials={task ? (task.trials ?? []) : null}
                  scopeLoading={overviewTrialsLoading}
                  // Panes with their own header render the verdict card there;
                  // the filesUrl-driven panes have no header, so the overview
                  // carries the verdict itself.
                  verdictTask={
                    checksDetail?.task ?? verdictTask ?? task ?? null
                  }
                  checksFindings={checksFindings}
                  checksStatus={checksVersion?.pre_trial_status}
                  checksError={checksVersion?.pre_trial_error}
                  onRerunChecks={handleRerunChecks}
                  checksRerunning={checksRerunning}
                  checksQueueError={checksQueueError}
                  checksLoading={checksLoading}
                  checksLoadError={checksLoadFailure}
                  qaActive={taskQaActive}
                  onOpenTrial={onOpenTrial}
                />
              ) : (
                renderFileContent()
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );

  const content = (
    <>
      <DrawerHeader className="border-border shrink-0 border-b px-4 py-3">
        <div className="mb-2 flex flex-wrap items-start justify-between gap-3 pr-20">
          <div className="min-w-0 flex-1">
            <DrawerTitle className="flex items-center gap-2 font-mono text-base font-semibold">
              <Button
                type="button"
                variant="ghost"
                onClick={handleCopyTaskName}
                className="h-auto max-w-full min-w-0 justify-start truncate bg-transparent p-0 text-left font-mono text-base font-semibold hover:bg-transparent hover:text-blue-400"
                title="Copy task name"
                aria-label={`Copy task name ${taskName}`}
              >
                {taskName}
              </Button>
              {showAnalysis !== false && currentVersion != null && (
                <span className="border-border bg-muted/50 text-muted-foreground inline-flex shrink-0 items-center rounded-md border px-1.5 py-0.5 font-mono text-[11px] font-medium">
                  v{currentVersion}
                </span>
              )}
            </DrawerTitle>
            <div className="mt-1 min-h-3 text-[10px] text-emerald-600">
              {copiedTaskName ? "Copied to clipboard" : null}
            </div>
          </div>
        </div>

        {/* Combined navigation row */}
        {(onNavigateToFirstTrial ||
          hasNavigation ||
          allowRetry ||
          canRunQA) && (
          <div className="text-muted-foreground space-y-2 pt-2 text-xs">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex flex-wrap items-center gap-3">
                {/* Task list navigation with position indicator */}
                {hasNavigation && (
                  <div className="flex items-center gap-1">
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      onClick={() => navigateTo(resolvedIndex - 1)}
                      disabled={!canGoPrev}
                      className="h-7 w-7"
                      aria-label="Previous task"
                      title="Previous task (↑)"
                    >
                      <ChevronUp className="h-4 w-4" />
                    </Button>
                    <span
                      className="text-muted-foreground min-w-[52px] px-1 text-center font-mono text-[11px] tabular-nums"
                      aria-label={`Task ${resolvedIndex + 1} of ${orderedList.length}`}
                      title={`Task ${resolvedIndex + 1} of ${orderedList.length}`}
                    >
                      {resolvedIndex + 1} / {orderedList.length}
                    </span>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      onClick={() => navigateTo(resolvedIndex + 1)}
                      disabled={!canGoNext}
                      className="h-7 w-7"
                      aria-label="Next task"
                      title="Next task (↓)"
                    >
                      <ChevronDown className="h-4 w-4" />
                    </Button>
                  </div>
                )}

                {/* Drill into this task's trials */}
                {onNavigateToFirstTrial && (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={onNavigateToFirstTrial}
                    className="h-7 gap-1 px-2 text-[10px] font-semibold tracking-wide uppercase"
                    aria-label="View trials for this task"
                    title="View trials (→)"
                  >
                    View trials
                    <ChevronRight className="h-3.5 w-3.5" />
                  </Button>
                )}
              </div>

              <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
                <div className="border-border bg-muted/30 rounded-md border px-3 py-1.5 text-right">
                  <div className="text-muted-foreground text-[9px] leading-none tracking-wider uppercase">
                    Avg score
                  </div>
                  <div className="mt-1 flex items-baseline justify-end gap-2">
                    <span className="font-mono text-sm leading-none font-semibold">
                      {averageRewardPct !== null ? `${averageRewardPct}%` : "—"}
                    </span>
                    <span className="text-muted-foreground text-[10px] leading-none">
                      {rewardTotal && rewardTotal > 0 && rewardSuccess != null
                        ? `${rewardSuccess.toFixed(2)}/${rewardTotal}`
                        : "No results"}
                    </span>
                  </div>
                </div>
                {canCancelTask && (
                  <Button
                    type="button"
                    variant="destructive"
                    size="sm"
                    onClick={handleCancelTask}
                    disabled={isCancelling}
                    className="h-7 px-2 text-[10px] font-semibold tracking-wide uppercase"
                  >
                    {isCancelling ? (
                      <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <OctagonX className="mr-1 h-3.5 w-3.5" />
                    )}
                    {isCancelling ? "Cancelling..." : cancelActionLabel}
                  </Button>
                )}
                {allowRetry && (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={handleRetryTask}
                    disabled={!canRetryTask || isRerunning}
                    title={
                      actionsReady ? undefined : "Loading latest task state."
                    }
                    className="h-7 px-2 text-[10px] font-semibold tracking-wide uppercase"
                  >
                    <RefreshCw
                      className={`mr-1 h-3.5 w-3.5 ${
                        isRerunning ? "animate-spin" : ""
                      }`}
                    />
                    {isRerunning ? "Rerunning..." : "Rerun trials"}
                  </Button>
                )}
                {showAnalysis && task && (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={handleRunQA}
                    disabled={!canRunQA || isRunningQA}
                    title={
                      actionsReady ? undefined : "Loading latest task state."
                    }
                    className="h-7 px-2 text-[10px] font-semibold tracking-wide uppercase"
                  >
                    {isRunningQA ? (
                      <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Microscope className="mr-1 h-3.5 w-3.5" />
                    )}
                    {isRunningQA ? "Queueing..." : qaActionLabel}
                  </Button>
                )}
              </div>
            </div>

            {(cancelError || rerunError || qaActionError) && (
              <div className="flex flex-wrap items-center justify-end gap-3 text-red-500">
                {cancelError && <span>{cancelError}</span>}
                {rerunError && <span>{rerunError}</span>}
                {qaActionError && <span>{qaActionError}</span>}
              </div>
            )}
          </div>
        )}
      </DrawerHeader>

      {/* The verdict lives in the task overview, not above the pane. As a
          `shrink-0` sibling of the scroll area it held its height forever:
          a long verdict (activiti's is ~1,600 characters across four
          recommendations) permanently ate half the pane, above BOTH the
          overview and the file view, and no amount of scrolling reached past
          it. TaskOverviewPanel renders it inline instead, so it scrolls with
          the section it belongs to and does not follow the reader into a
          file. */}
      <div className="flex flex-1 flex-col overflow-hidden">
        {fileTreeContent}
      </div>
    </>
  );

  if (contentOnly) {
    if (filesUrl) {
      return (
        <div className="flex h-full flex-1 flex-col overflow-hidden">
          {fileTreeContent}
        </div>
      );
    }
    return (
      <div className="flex h-full flex-1 flex-col overflow-hidden">
        {content}
      </div>
    );
  }

  return (
    <ResizableDrawer
      open={isOpen}
      onOpenChange={(open) => !open && onClose()}
      defaultWidth={650}
      minWidth={400}
      maxWidth={1200}
    >
      {content}
    </ResizableDrawer>
  );
}
