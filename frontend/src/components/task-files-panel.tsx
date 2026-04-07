"use client";

import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import useSWR from "swr";
import {
  ResizableDrawer,
  DrawerHeader,
  DrawerTitle,
} from "@/components/ui/resizable-drawer";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Folder,
  FolderOpen,
  File,
  FileText,
  FileCode,
  ChevronLeft,
  ChevronRight,
  ChevronDown,
  ChevronUp,
  RefreshCw,
  AlertCircle,
  Microscope,
  CheckCircle2,
  AlertTriangle,
  XCircle,
  Loader2,
  OctagonX,
} from "lucide-react";
import { fetcher } from "@/lib/api";
import { CodeBlock, getLanguageFromFilename } from "@/components/code-block";
import type { Task, Trial } from "@/lib/types";

interface TaskFile {
  path: string;
  key: string;
  content?: string;
  size?: number;
  last_modified?: string;
  url?: string; // Presigned S3 URL for direct access
}

interface TaskDirectory {
  path: string;
}

interface FilesListingResponse {
  files?: TaskFile[];
  dirs?: TaskDirectory[];
}

interface TreeNode {
  name: string;
  path: string;
  type: "file" | "dir";
  children?: TreeNode[];
  content?: string;
  url?: string; // Presigned S3 URL for direct access
  size?: number; // File size in bytes
  isLoaded?: boolean;
  isTruncated?: boolean; // True if content was truncated due to size
}

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
  allowRetry?: boolean;
  onRetryComplete?: (taskIds?: string[]) => void;
  /** Render content only without ResizableDrawer wrapper */
  contentOnly?: boolean;
  /**
   * Override the files URL base (e.g. `/api/trials/{id}/files`).
   * When set, the component fetches the listing from `${filesUrl}?recursive=1`
   * and individual file content from `${filesUrl}/${path}`.
   * This allows reusing the file tree viewer for trial files.
   */
  filesUrl?: string;
  /**
   * When set, auto-expand the tree to this file path and select it.
   * Useful for deep-linking from external UI (e.g. execution timeline).
   * Bump the value or pair with a counter to re-trigger navigation to the same path.
   */
  initialFilePath?: string | null;
}

function getNodeName(path: string): string {
  const parts = path.split("/").filter(Boolean);
  return parts[parts.length - 1] || path;
}

// Truncate files larger than 100KB initially
const TRUNCATE_THRESHOLD = 100 * 1024;

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function buildNodesFromListing(
  files: TaskFile[] = [],
  dirs: TaskDirectory[] = [],
): TreeNode[] {
  const dirNodes = dirs.map((dir) => ({
    name: getNodeName(dir.path),
    path: dir.path,
    type: "dir" as const,
    children: [],
    isLoaded: false,
  }));
  const fileNodes = files.map((file) => ({
    name: getNodeName(file.path),
    path: file.path,
    type: "file" as const,
    content: file.content,
    url: file.url,
    size: file.size,
  }));
  const sortedDirs = dirNodes.sort((a, b) => a.name.localeCompare(b.name));
  const sortedFiles = fileNodes.sort((a, b) => a.name.localeCompare(b.name));
  return [...sortedDirs, ...sortedFiles];
}

function buildTreeFromPaths(files: TaskFile[]): TreeNode[] {
  const root: TreeNode[] = [];
  const dirMap = new Map<string, TreeNode>();

  const sortedFiles = [...files].sort((a, b) => a.path.localeCompare(b.path));

  for (const file of sortedFiles) {
    const parts = file.path.split("/").filter(Boolean);
    if (parts.length === 0) continue;

    let currentLevel = root;
    let currentPath = "";

    for (let i = 0; i < parts.length - 1; i++) {
      const dirName = parts[i];
      currentPath = currentPath ? `${currentPath}/${dirName}` : dirName;

      let dir = dirMap.get(currentPath);
      if (!dir) {
        dir = {
          name: dirName,
          path: currentPath,
          type: "dir",
          children: [],
          isLoaded: true,
        };
        dirMap.set(currentPath, dir);
        currentLevel.push(dir);
      }
      currentLevel = dir.children!;
    }

    const fileName = parts[parts.length - 1];
    currentLevel.push({
      name: fileName,
      path: file.path,
      type: "file",
      content: file.content,
      url: file.url,
      size: file.size,
    });
  }

  return root;
}

function updateTree(
  nodes: TreeNode[],
  targetPath: string,
  updater: (node: TreeNode) => TreeNode,
): TreeNode[] {
  return nodes.map((node) => {
    if (node.path === targetPath) {
      return updater(node);
    }
    if (node.type === "dir" && node.children) {
      return {
        ...node,
        children: updateTree(node.children, targetPath, updater),
      };
    }
    return node;
  });
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

function isTextContent(contentType: string): boolean {
  const normalized = contentType.toLowerCase();
  return (
    normalized.startsWith("text/") ||
    normalized.includes("json") ||
    normalized.includes("yaml") ||
    normalized.includes("toml") ||
    normalized.includes("xml") ||
    normalized.includes("javascript") ||
    normalized.includes("typescript")
  );
}

export function TaskFilesPanel({
  isOpen,
  onClose,
  taskId,
  task,
  orderedTasks,
  taskIndex,
  onNavigate,
  onNavigateToFirstTrial,
  apiBaseUrl,
  allowRetry = true,
  onRetryComplete,
  contentOnly = false,
  filesUrl,
  initialFilePath,
}: TaskFilesPanelProps) {
  const baseUrl = apiBaseUrl ?? "/api";
  const resolvedFilesUrl = filesUrl ?? `${baseUrl}/tasks/${taskId}/files`;
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isRerunning, setIsRerunning] = useState(false);
  const [rerunError, setRerunError] = useState<string | null>(null);
  const [isCancelling, setIsCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [isRunningAnalysis, setIsRunningAnalysis] = useState(false);
  const [analysisActionError, setAnalysisActionError] = useState<string | null>(
    null,
  );
  const [isRunningVerdict, setIsRunningVerdict] = useState(false);
  const [verdictActionError, setVerdictActionError] = useState<string | null>(
    null,
  );
  const [fileTree, setFileTree] = useState<TreeNode[]>([]);
  const [expandedDirs, setExpandedDirs] = useState<Set<string>>(new Set());
  const [selectedFile, setSelectedFile] = useState<TreeNode | null>(null);
  const [fileContent, setFileContent] = useState<string | null>(null);
  const [fileContentLoading, setFileContentLoading] = useState(false);
  const [loadingDirs, setLoadingDirs] = useState<Set<string>>(new Set());
  const [isTruncated, setIsTruncated] = useState(false);
  const [fullFileSize, setFullFileSize] = useState<number | null>(null);
  const [loadingFullFile, setLoadingFullFile] = useState(false);
  const [copiedTaskName, setCopiedTaskName] = useState(false);
  const contentRef = useRef<HTMLDivElement>(null);
  const copiedTaskNameTimeoutRef = useRef<number | null>(null);
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
  const recursiveFilesKey =
    isOpen && filesUrl ? `${resolvedFilesUrl}?recursive=1` : null;
  const {
    data: recursiveFilesData,
    error: recursiveFilesError,
    isLoading: recursiveFilesLoading,
  } = useSWR<FilesListingResponse>(recursiveFilesKey, fetcher, {
    revalidateOnFocus: false,
  });
  const currentVersion = (verdictTask ?? task)?.current_version ?? null;
  const versionSuffix = currentVersion != null ? `&version=${currentVersion}` : "";

  const verdictSource = verdictTask ?? task;

  const orderedList = useMemo(() => orderedTasks ?? [], [orderedTasks]);
  const resolvedIndex =
    typeof taskIndex === "number" && taskIndex >= 0
      ? taskIndex
      : orderedList.findIndex((item) => item.id === taskId);
  const hasNavigation =
    Boolean(onNavigate) && orderedList.length > 1 && resolvedIndex >= 0;
  const canGoPrev = hasNavigation && resolvedIndex > 0;
  const canGoNext = hasNavigation && resolvedIndex < orderedList.length - 1;
  const progressPct =
    hasNavigation && orderedList.length > 1
      ? (resolvedIndex / (orderedList.length - 1)) * 100
      : 0;

  const retryableTrials = useMemo(() => {
    if (!task?.trials) return [];
    return task.trials.filter(
      (trial) => trial.status === "failed" || trial.status === "success",
    );
  }, [task]);

  const canRetryTask = allowRetry && retryableTrials.length > 0;
  const activeTrials = useMemo(() => {
    if (!task?.trials) return [];
    return task.trials.filter((trial) =>
      ["running", "queued", "retrying", "pending"].includes(trial.status),
    );
  }, [task]);
  const canCancelTask = allowRetry && activeTrials.length > 0;
  const allTrialsTerminal =
    Boolean(task?.trials?.length) &&
    (task?.trials ?? []).every(
      (trial) => trial.status === "failed" || trial.status === "success",
    );
  const hasAnalysisInFlight = (task?.trials ?? []).some((trial) =>
    ["pending", "queued", "running"].includes(trial.analysis_status ?? ""),
  );
  const allAnalysesComplete =
    Boolean(task?.trials?.length) &&
    (task?.trials ?? []).every(
      (trial) =>
        trial.analysis_status === "success" ||
        trial.analysis_status === "failed",
    );
  const verdictInFlight = ["pending", "queued", "running"].includes(
    verdictSource?.verdict_status ?? "",
  );
  const canRunTaskAnalysis =
    allowRetry &&
    Boolean(task) &&
    allTrialsTerminal &&
    !hasAnalysisInFlight &&
    !verdictInFlight;
  const canRunVerdict =
    allowRetry &&
    Boolean(task) &&
    allTrialsTerminal &&
    allAnalysesComplete &&
    !verdictInFlight;
  const analysisActionLabel = (task?.trials ?? []).some(
    (trial) => trial.analysis_status || trial.analysis,
  )
    ? "Rerun analyses"
    : "Run analyses";
  const verdictActionLabel =
    verdictSource?.verdict_status || verdictSource?.verdict
      ? "Rerun verdict"
      : "Run verdict";

  const navigateTo = useCallback(
    (nextIndex: number) => {
      if (!onNavigate) return;
      const nextTask = orderedList[nextIndex];
      if (!nextTask) return;
      onNavigate(nextTask, nextIndex);
    },
    [onNavigate, orderedList],
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
              data.detail || data.error || "Failed to retry trial",
            );
          }
        }),
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
      const res = await fetch(`${baseUrl}/tasks/cancel`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task_ids: id ? [id] : [] }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || data.error || "Failed to cancel task");
      }
      setCancelError(null);
      onRetryComplete?.(id ? [id] : undefined);
    } catch (err) {
      setCancelError(
        err instanceof Error ? err.message : "Failed to cancel task",
      );
    } finally {
      setIsCancelling(false);
    }
  };

  const handleRunTaskAnalysis = async () => {
    if (!task?.id || !canRunTaskAnalysis || isRunningAnalysis) return;
    setIsRunningAnalysis(true);
    setAnalysisActionError(null);

    try {
      const res = await fetch(`${baseUrl}/tasks/${task.id}/analysis/retry`, {
        method: "POST",
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(
          data.detail || data.error || "Failed to queue task analysis",
        );
      }
      onRetryComplete?.([task.id]);
    } catch (err) {
      setAnalysisActionError(
        err instanceof Error ? err.message : "Failed to queue task analysis",
      );
    } finally {
      setIsRunningAnalysis(false);
    }
  };

  const handleRunVerdict = async () => {
    if (!task?.id || !canRunVerdict || isRunningVerdict) return;
    setIsRunningVerdict(true);
    setVerdictActionError(null);

    try {
      const res = await fetch(`${baseUrl}/tasks/${task.id}/verdict/retry`, {
        method: "POST",
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || data.error || "Failed to queue verdict");
      }
      onRetryComplete?.([task.id]);
    } catch (err) {
      setVerdictActionError(
        err instanceof Error ? err.message : "Failed to queue verdict",
      );
    } finally {
      setIsRunningVerdict(false);
    }
  };

  useEffect(() => {
    setRerunError(null);
    setIsRerunning(false);
    setAnalysisActionError(null);
    setIsRunningAnalysis(false);
    setVerdictActionError(null);
    setIsRunningVerdict(false);
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

  useEffect(() => {
    if (!isOpen || !filesUrl) {
      return;
    }

    setError(null);
    setFileTree([]);
    setSelectedFile(null);
    setFileContent(null);
    setExpandedDirs(new Set());
  }, [isOpen, filesUrl]);

  useEffect(() => {
    if (!isOpen || !filesUrl) {
      return;
    }

    if (!recursiveFilesData) {
      return;
    }

    const files: TaskFile[] = recursiveFilesData.files || [];
    const tree = buildTreeFromPaths(files);
    setFileTree(tree);
    setSelectedFile((prev) => {
      if (prev) {
        return findNodeByPath(tree, prev.path) ?? prev;
      }
      return findFirstFile(tree);
    });
  }, [isOpen, filesUrl, recursiveFilesData]);

  // Fetch task file list when panel opens
  useEffect(() => {
    if (!isOpen || !taskId || filesUrl) {
      return;
    }

    let cancelled = false;

    async function fetchFiles() {
      setLoading(true);
      setError(null);
      setFileTree([]);
      setSelectedFile(null);
      setFileContent(null);
      setExpandedDirs(new Set());

      try {
        const res = await fetch(`${resolvedFilesUrl}?recursive=0${versionSuffix}`);
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          throw new Error(
            data.detail || `Failed to fetch files: ${res.statusText}`,
          );
        }
        const data = await res.json();

        if (cancelled) return;

        const files: TaskFile[] = data.files || [];
        const dirs: TaskDirectory[] = data.dirs || [];
        const tree = buildNodesFromListing(files, dirs);
        setFileTree(tree);
        const firstFile = findFirstFile(tree);
        if (firstFile) {
          setSelectedFile(firstFile);
        }
      } catch (err) {
        if (!cancelled) {
          setError(
            err instanceof Error ? err.message : "Failed to fetch files",
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
    };
  }, [isOpen, taskId, filesUrl, resolvedFilesUrl, versionSuffix]);

  const loadDirectory = useCallback(
    async (path: string) => {
      if (!taskId && !filesUrl) return;
      setLoadingDirs((prev) => new Set(prev).add(path));
      try {
        const prefix = encodeURIComponent(path);
        const res = await fetch(
          `${resolvedFilesUrl}?recursive=0&prefix=${prefix}${versionSuffix}`,
        );
        if (!res.ok) {
          throw new Error("Failed to fetch directory");
        }
        const data = await res.json();
        const files: TaskFile[] = data.files || [];
        const dirs: TaskDirectory[] = data.dirs || [];
        const children = buildNodesFromListing(files, dirs);
        setFileTree((prev) =>
          updateTree(prev, path, (node) => ({
            ...node,
            children,
            isLoaded: true,
          })),
        );
      } catch (err) {
        setError(
          err instanceof Error ? err.message : "Failed to fetch directory",
        );
      } finally {
        setLoadingDirs((prev) => {
          const next = new Set(prev);
          next.delete(path);
          return next;
        });
      }
    },
    [taskId, filesUrl, resolvedFilesUrl, versionSuffix],
  );

  // Fetch file content when a file is selected
  useEffect(() => {
    if (
      !selectedFile ||
      selectedFile.type !== "file" ||
      (!taskId && !filesUrl)
    ) {
      return;
    }

    // If we already have content cached in the node, use it
    if (selectedFile.content !== undefined) {
      setFileContent(selectedFile.content);
      setIsTruncated(selectedFile.isTruncated || false);
      setFullFileSize(selectedFile.size || null);
      return;
    }

    // Capture values for async function
    const filePath = selectedFile.path;
    const fileNode = selectedFile;
    const presignedUrl = selectedFile.url;
    const fileSize = selectedFile.size;
    const shouldTruncate = fileSize && fileSize > TRUNCATE_THRESHOLD;
    let cancelled = false;

    async function fetchContent() {
      setFileContentLoading(true);
      setIsTruncated(false);
      setFullFileSize(fileSize || null);

      try {
        let content: string | null = null;
        let truncated = false;

        // Use presigned URL directly from listing if available (fast path)
        if (presignedUrl) {
          try {
            // For large files, use Range header to fetch only first chunk
            const headers: HeadersInit = shouldTruncate
              ? { Range: `bytes=0-${TRUNCATE_THRESHOLD - 1}` }
              : {};

            const s3Res = await fetch(presignedUrl, { headers });

            // 206 = Partial Content (Range request succeeded)
            // 200 = Full content (Range not supported or file smaller than range)
            if (s3Res.ok || s3Res.status === 206) {
              const contentType = s3Res.headers.get("content-type") || "";
              if (isTextContent(contentType)) {
                content = await s3Res.text();
                // Check if we got partial content
                truncated =
                  s3Res.status === 206 ||
                  (!!shouldTruncate && content.length >= TRUNCATE_THRESHOLD);
              } else {
                content = `Binary file (content-type: ${contentType || "unknown"})`;
              }
            }
          } catch {
            content = null;
          }
        }

        // Fallback: fetch via backend proxy (slower, but works if presigned URL expired)
        if (content === null) {
          const encodedPath = encodeURIComponent(filePath);
          const versionParam = currentVersion != null ? `?version=${currentVersion}` : "";
          const res = await fetch(`${resolvedFilesUrl}/${encodedPath}${versionParam}`);
          if (!res.ok) {
            throw new Error("Failed to fetch file content");
          }
          if (filesUrl) {
            content = await res.text();
          } else {
            const data = await res.json();
            content = data.content || "";
          }
        }

        if (!cancelled) {
          setFileContent(content || "");
          setIsTruncated(truncated);
          // Cache in the node
          fileNode.content = content || "";
          fileNode.isTruncated = truncated;
        }
      } catch {
        if (!cancelled) {
          setFileContent("Error loading file content");
        }
      } finally {
        if (!cancelled) {
          setFileContentLoading(false);
        }
      }
    }

    fetchContent();

    return () => {
      cancelled = true;
    };
  }, [selectedFile, taskId, filesUrl, resolvedFilesUrl, currentVersion]);

  // Load full file content (when user clicks "Load full file")
  const loadFullFile = useCallback(async () => {
    if (!selectedFile || !selectedFile.url || !taskId) return;

    setLoadingFullFile(true);
    try {
      const s3Res = await fetch(selectedFile.url);
      if (s3Res.ok) {
        const contentType = s3Res.headers.get("content-type") || "";
        if (isTextContent(contentType)) {
          const content = await s3Res.text();
          setFileContent(content);
          setIsTruncated(false);
          // Update cache
          selectedFile.content = content;
          selectedFile.isTruncated = false;
        }
      }
    } catch {
      // Keep truncated content on error
    } finally {
      setLoadingFullFile(false);
    }
  }, [selectedFile, taskId]);

  // Scroll to top when selected file changes
  useEffect(() => {
    if (contentRef.current) {
      contentRef.current.scrollTop = 0;
    }
  }, [selectedFile]);

  // Reset state when panel closes or task changes
  useEffect(() => {
    if (!isOpen) {
      setFileTree([]);
      setSelectedFile(null);
      setFileContent(null);
      setError(null);
      setExpandedDirs(new Set());
      setIsTruncated(false);
      setFullFileSize(null);
      setLoadingFullFile(false);
      setAnalysisActionError(null);
      setIsRunningAnalysis(false);
      setVerdictActionError(null);
      setIsRunningVerdict(false);
    }
  }, [isOpen, taskId]);

  // Navigate to a specific file when initialFilePath changes (suffix match)
  useEffect(() => {
    if (!initialFilePath || fileTree.length === 0) return;

    const node =
      findNodeByPath(fileTree, initialFilePath) ??
      findNodeBySuffix(fileTree, initialFilePath);
    if (!node || node.type !== "file") return;

    const parts = node.path.split("/").filter(Boolean);
    if (parts.length > 1) {
      setExpandedDirs((prev) => {
        const next = new Set(prev);
        let accumulated = "";
        for (let i = 0; i < parts.length - 1; i++) {
          accumulated = accumulated ? `${accumulated}/${parts[i]}` : parts[i];
          next.add(accumulated);
        }
        return next;
      });
    }

    setSelectedFile(node);
  }, [initialFilePath, fileTree]);

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

  const toggleDir = useCallback((path: string) => {
    setExpandedDirs((prev) => {
      const next = new Set(prev);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  }, []);

  const renderFileTree = (nodes: TreeNode[], depth = 0) => {
    return nodes.map((node) => {
      const isExpanded = expandedDirs.has(node.path);
      const isSelected = selectedFile?.path === node.path;
      const isLoadingDir = loadingDirs.has(node.path);
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
                const willExpand = !isExpanded;
                toggleDir(node.path);
                if (willExpand && !node.isLoaded && !isLoadingDir) {
                  void loadDirectory(node.path);
                }
              } else {
                setSelectedFile(node);
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
                  <ChevronDown className="h-3 w-3 text-muted-foreground" />
                ) : (
                  <ChevronRight className="h-3 w-3 text-muted-foreground" />
                )}
              </span>
            )}
            {node.type === "file" && <span className="w-3" />}
            <Icon
              className={`h-4 w-4 flex-shrink-0 ${
                node.type === "dir"
                  ? "text-yellow-500"
                  : "text-muted-foreground"
              }`}
            />
            {node.type === "dir" && isLoadingDir && (
              <Loader2 className="h-3 w-3 animate-spin text-muted-foreground" />
            )}
            <span className="truncate">{node.name}</span>
          </Button>
          {node.type === "dir" && isExpanded && node.children && (
            <div>{renderFileTree(node.children, depth + 1)}</div>
          )}
        </div>
      );
    });
  };

  const renderFileContent = () => {
    if (!selectedFile) {
      return (
        <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
          Select a file to view its contents
        </div>
      );
    }

    if (fileContentLoading) {
      return (
        <div className="space-y-2 p-4">
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-3/4" />
          <Skeleton className="h-4 w-5/6" />
        </div>
      );
    }

    if (fileContent === null) {
      return (
        <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
          Unable to load file content
        </div>
      );
    }

    const language = getLanguageFromFilename(selectedFile.name);

    return (
      <div className="flex h-full flex-col">
        <CodeBlock
          code={fileContent}
          language={language}
          className="min-h-0 flex-1"
          maxHeight="none"
          truncateAt={0}
        />
        {isTruncated && (
          <div className="flex items-center justify-between border-t border-border bg-muted/50 px-4 py-3">
            <span className="text-xs text-muted-foreground">
              Showing first {formatFileSize(TRUNCATE_THRESHOLD)} of{" "}
              {fullFileSize ? formatFileSize(fullFileSize) : "large file"}
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
    return () => {
      if (copiedTaskNameTimeoutRef.current !== null) {
        window.clearTimeout(copiedTaskNameTimeoutRef.current);
      }
    };
  }, []);

  const { rewardSuccess, rewardTotal, averageRewardPct } = useMemo(() => {
    const trials = task?.trials ?? [];
    const versionTrials =
      currentVersion != null
        ? trials.filter((t) => t.task_version === currentVersion)
        : trials;
    const success = versionTrials.filter((t) => t.reward === 1).length;
    const total = versionTrials.filter((t) => t.reward != null).length;
    return {
      rewardSuccess: total > 0 ? success : null,
      rewardTotal: total > 0 ? total : null,
      averageRewardPct:
        total > 0 ? Math.round((success / total) * 100) : null,
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

  const showVerdictCard =
    Boolean(verdictSource) &&
    Boolean(verdictSource?.verdict_status || verdictSource?.verdict);
  const verdictReasoning = verdictSource?.verdict?.reasoning?.trim() || null;

  const isListingLoading = filesUrl ? recursiveFilesLoading : loading;
  const listingError =
    filesUrl && recursiveFilesError ? recursiveFilesError.message : error;

  const fileTreeContent = (
    <>
      {isListingLoading ? (
        <div className="flex flex-1 items-center justify-center">
          <div className="flex items-center gap-2 text-muted-foreground">
            <Loader2 className="h-5 w-5 animate-spin" />
            <span className="text-sm">Loading files...</span>
          </div>
        </div>
      ) : listingError ? (
        <div className="flex flex-1 items-center justify-center p-4 sm:p-6">
          <div className="space-y-2 text-center">
            <AlertCircle className="mx-auto h-8 w-8 text-red-500" />
            <p className="text-sm text-muted-foreground">
              Unable to load files
            </p>
            <p className="text-xs text-muted-foreground">{listingError}</p>
          </div>
        </div>
      ) : fileTree.length === 0 ? (
        <div className="flex flex-1 items-center justify-center p-4 sm:p-6">
          <div className="space-y-2 text-center">
            <p className="text-sm text-muted-foreground">No files found</p>
            {!filesUrl && (
              <p className="text-xs text-muted-foreground">
                The task directory may be empty or not uploaded to S3
              </p>
            )}
          </div>
        </div>
      ) : (
        <div className="flex flex-1 flex-col overflow-hidden md:flex-row">
          <div className="max-h-[30vh] w-full overflow-auto border-b border-border bg-muted/30 md:max-h-none md:w-56 md:border-b-0 md:border-r lg:w-64">
            <div className="p-2">
              <div className="px-2 py-2 font-mono text-[10px] font-semibold uppercase tracking-wide text-muted-foreground sm:text-xs">
                Files
              </div>
              {renderFileTree(fileTree)}
            </div>
          </div>
          <div className="flex flex-1 flex-col overflow-hidden">
            {selectedFile && (
              <div className="border-b border-border bg-muted/30 px-3 py-2 sm:px-4">
                <div className="truncate font-mono text-[10px] text-muted-foreground sm:text-xs">
                  {selectedFile.path}
                </div>
              </div>
            )}
            <div ref={contentRef} className="flex-1 overflow-auto bg-card">
              {renderFileContent()}
            </div>
          </div>
        </div>
      )}
    </>
  );

  const content = (
    <>
      <DrawerHeader className="shrink-0 border-b border-border px-4 py-3">
        <div className="mb-2 flex flex-wrap items-start justify-between gap-3 pr-20">
          <div className="min-w-0 flex-1">
            <DrawerTitle className="flex items-center gap-2 font-mono text-base font-semibold">
              <button
                type="button"
                onClick={handleCopyTaskName}
                className="block min-w-0 max-w-full truncate text-left transition hover:text-blue-400"
                title="Copy task name"
                aria-label={`Copy task name ${taskName}`}
              >
                {taskName}
              </button>
              {currentVersion != null && (
                <span className="inline-flex shrink-0 items-center rounded-md border border-border bg-muted/50 px-1.5 py-0.5 font-mono text-[11px] font-medium text-muted-foreground">
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
          canRunTaskAnalysis ||
          canRunVerdict) && (
          <div className="space-y-2 pt-2 text-xs text-muted-foreground">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex flex-wrap items-center gap-3">
                {/* Horizontal navigation - task icon view */}
                {onNavigateToFirstTrial && (
                  <div className="flex items-center gap-1">
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      disabled={true}
                      className="h-7 w-7"
                      aria-label="No previous"
                    >
                      <ChevronLeft className="h-4 w-4" />
                    </Button>

                    {/* Task indicator - active since we're viewing the task */}
                    <div className="flex gap-1">
                      <span
                        className="flex h-5 w-5 items-center justify-center rounded-sm border border-blue-500 bg-blue-500 text-[10px] font-bold text-white ring-2 ring-primary ring-offset-1 ring-offset-background transition"
                        aria-label="Current: Task view"
                        title="Task view"
                      >
                        <FileText className="h-3 w-3" />
                      </span>
                    </div>

                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      onClick={onNavigateToFirstTrial}
                      className="h-7 w-7"
                      aria-label="View first trial"
                      title="View first trial"
                    >
                      <ChevronRight className="h-4 w-4" />
                    </Button>
                  </div>
                )}

                {/* Vertical navigation - task list position */}
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
                      title="Previous task"
                    >
                      <ChevronUp className="h-4 w-4" />
                    </Button>
                    <div
                      className="relative h-6 w-1.5 rounded-full bg-muted"
                      aria-label="Task position"
                      title="Task position"
                    >
                      <div
                        className="absolute left-0 right-0 rounded-full bg-primary"
                        style={{ height: `${progressPct}%`, bottom: 0 }}
                      />
                    </div>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      onClick={() => navigateTo(resolvedIndex + 1)}
                      disabled={!canGoNext}
                      className="h-7 w-7"
                      aria-label="Next task"
                      title="Next task"
                    >
                      <ChevronDown className="h-4 w-4" />
                    </Button>
                  </div>
                )}
              </div>

              <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
                <div className="rounded-md border border-border bg-muted/30 px-3 py-1.5 text-right">
                  <div className="text-[9px] uppercase leading-none tracking-wider text-muted-foreground">
                    Avg reward
                  </div>
                  <div className="mt-1 flex items-baseline justify-end gap-2">
                    <span className="font-mono text-sm font-semibold leading-none">
                      {averageRewardPct !== null ? `${averageRewardPct}%` : "—"}
                    </span>
                    <span className="text-[10px] leading-none text-muted-foreground">
                      {rewardTotal && rewardTotal > 0 && rewardSuccess != null
                        ? `${rewardSuccess}/${rewardTotal}`
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
                    className="h-7 px-2 text-[10px] font-semibold uppercase tracking-wide"
                  >
                    {isCancelling ? (
                      <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <OctagonX className="mr-1 h-3.5 w-3.5" />
                    )}
                    {isCancelling
                      ? "Cancelling..."
                      : `Cancel (${activeTrials.length})`}
                  </Button>
                )}
                {allowRetry && (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={handleRetryTask}
                    disabled={!canRetryTask || isRerunning}
                    className="h-7 px-2 text-[10px] font-semibold uppercase tracking-wide"
                  >
                    <RefreshCw
                      className={`mr-1 h-3.5 w-3.5 ${
                        isRerunning ? "animate-spin" : ""
                      }`}
                    />
                    {isRerunning ? "Rerunning..." : "Rerun trials"}
                  </Button>
                )}
                {task && (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={handleRunTaskAnalysis}
                    disabled={!canRunTaskAnalysis || isRunningAnalysis}
                    className="h-7 px-2 text-[10px] font-semibold uppercase tracking-wide"
                  >
                    {isRunningAnalysis ? (
                      <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Microscope className="mr-1 h-3.5 w-3.5" />
                    )}
                    {isRunningAnalysis ? "Queueing..." : analysisActionLabel}
                  </Button>
                )}
                {task && (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={handleRunVerdict}
                    disabled={!canRunVerdict || isRunningVerdict}
                    className="h-7 px-2 text-[10px] font-semibold uppercase tracking-wide"
                  >
                    {isRunningVerdict ? (
                      <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <CheckCircle2 className="mr-1 h-3.5 w-3.5" />
                    )}
                    {isRunningVerdict ? "Queueing..." : verdictActionLabel}
                  </Button>
                )}
              </div>
            </div>

            {(cancelError ||
              rerunError ||
              analysisActionError ||
              verdictActionError) && (
              <div className="flex flex-wrap items-center justify-end gap-3 text-red-500">
                {cancelError && <span>{cancelError}</span>}
                {rerunError && <span>{rerunError}</span>}
                {analysisActionError && <span>{analysisActionError}</span>}
                {verdictActionError && <span>{verdictActionError}</span>}
              </div>
            )}
          </div>
        )}
      </DrawerHeader>

      <div className="flex flex-1 flex-col overflow-hidden">
        {showVerdictCard && (
          <div className="flex-shrink-0 border-b border-border bg-muted/10">
            <div className="p-4 sm:p-6">
              <Card
                className={
                  verdictSource?.verdict_status === "running" ||
                  verdictSource?.verdict_status === "pending" ||
                  verdictSource?.verdict_status === "queued"
                    ? "border-blue-500/30 bg-blue-500/5"
                    : verdictSource?.verdict?.is_good
                      ? "border-emerald-500/30 bg-emerald-500/5"
                      : verdictSource?.verdict?.is_good === false
                        ? "border-amber-500/30 bg-amber-500/5"
                        : verdictSource?.verdict_status === "failed"
                          ? "border-red-500/30 bg-red-500/5"
                          : "border-slate-500/30 bg-slate-500/5"
                }
              >
                <CardHeader className="px-4 pb-1 pt-2">
                  <CardTitle className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                    <Microscope className="h-3 w-3" />
                    Task Verdict
                  </CardTitle>
                </CardHeader>
                <CardContent className="px-4 pb-3">
                  <div className="flex items-start gap-3">
                    {verdictSource?.verdict_status === "running" ||
                    verdictSource?.verdict_status === "pending" ||
                    verdictSource?.verdict_status === "queued" ? (
                      <Loader2 className="mt-0.5 h-5 w-5 animate-spin text-blue-500" />
                    ) : verdictSource?.verdict?.is_good ? (
                      <CheckCircle2 className="mt-0.5 h-5 w-5 text-emerald-500" />
                    ) : verdictSource?.verdict?.is_good === false ? (
                      <AlertTriangle className="mt-0.5 h-5 w-5 text-amber-500" />
                    ) : verdictSource?.verdict_status === "failed" ? (
                      <XCircle className="mt-0.5 h-5 w-5 text-red-500" />
                    ) : (
                      <Microscope className="mt-0.5 h-5 w-5 text-slate-500" />
                    )}
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-sm font-bold">
                          {verdictSource?.verdict_status === "running" ||
                          verdictSource?.verdict_status === "pending" ||
                          verdictSource?.verdict_status === "queued"
                            ? "Computing verdict..."
                            : verdictSource?.verdict_status === "failed"
                              ? "Verdict Failed"
                              : verdictSource?.verdict?.is_good
                                ? "Task is Good"
                                : verdictSource?.verdict?.is_good === false
                                  ? "Needs Review"
                                  : "Verdict Pending"}
                        </span>
                        {verdictSource?.verdict?.confidence && (
                          <span className="text-xs text-muted-foreground">
                            · {verdictSource.verdict.confidence} confidence
                          </span>
                        )}
                      </div>
                      {verdictSource?.verdict?.is_good && verdictReasoning && (
                        <p className="mt-1 text-xs text-muted-foreground">
                          {verdictReasoning}
                        </p>
                      )}
                      {verdictSource?.verdict?.primary_issue &&
                        verdictSource?.verdict?.is_good === false && (
                        <p className="mt-1 text-xs text-muted-foreground">
                          {verdictSource.verdict.primary_issue}
                        </p>
                      )}
                      {verdictSource?.verdict?.recommendations &&
                        verdictSource.verdict.recommendations.length > 0 && (
                          <div className="mt-2 space-y-1">
                            {verdictSource.verdict.recommendations.map(
                              (rec: string, idx: number) => (
                                <p
                                  key={idx}
                                  className="text-xs italic text-muted-foreground/80"
                                >
                                  💡 {rec}
                                </p>
                              ),
                            )}
                          </div>
                        )}
                      {verdictSource?.verdict_status === "failed" &&
                        verdictSource.verdict_error && (
                          <p className="mt-1 text-xs text-red-500">
                            {verdictSource.verdict_error}
                          </p>
                        )}
                    </div>
                  </div>
                </CardContent>
              </Card>
            </div>
          </div>
        )}

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
