"use client";

import { useState, useEffect, useCallback, useMemo } from "react";
import useSWR from "swr";
import {
  ResizableDrawer,
  DrawerHeader,
  DrawerTitle,
  DrawerDescription,
} from "@/components/ui/resizable-drawer";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Clock,
  FileText,
  Terminal,
  AlertCircle,
  ChevronDown,
  ChevronUp,
  ChevronLeft,
  ChevronRight,
  Ban,
  RotateCcw,
  Loader2,
  Microscope,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Route,
  Bot,
  FlaskConical,
  Flame,
  FolderOpen,
  Folder,
  File,
  FileCode,
  Trophy,
  Layers,
  ChevronRight as ChevronRightIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { TrajectoryViewer } from "@/components/trajectory-viewer";
import { TrialMetricsCards } from "@/components/trial-metrics-cards";
import type { Trial, Task } from "@/lib/types";
import { getMatrixStatus, STATUS_CONFIG } from "@/lib/status-config";
import { fetcher } from "@/lib/api";
import { HarborStageTimeline } from "@/components/harbor-stage-timeline";
import { HarborStageBadge } from "@/components/harbor-stage-badge";
import { StatusBadge } from "@/components/status-badge";

// =============================================================================
// Types
// =============================================================================

interface AgentGroup {
  agent: string;
  model: string | null;
  trials: Trial[];
  passCount: number;
  failCount: number;
  errorCount: number;
  avgDuration: number | null;
}

interface StructuredLogs {
  trial_id: string;
  agent: {
    oracle: string | null;
    setup: string | null;
    commands: Array<{ name: string; content: string }>;
  };
  verifier: {
    stdout: string | null;
    stderr: string | null;
  };
  other: Array<{ name: string; content: string }>;
  exception: string | null;
}

type LogCategory = "agent" | "verifier" | "other" | "exception";

// File tree types
interface TaskFile {
  path: string;
  key: string;
  content?: string;
  size?: number;
  url?: string;
}

interface TreeNode {
  name: string;
  path: string;
  type: "file" | "dir";
  children?: TreeNode[];
  content?: string;
  url?: string;
  size?: number;
  isLoaded?: boolean;
}

type LeftPanelTab = "trials" | "files";

// =============================================================================
// Constants
// =============================================================================

// =============================================================================
// Utility Functions
// =============================================================================

function formatDuration(start?: string | null, end?: string | null): string {
  if (!start) return "—";
  const startDate = new Date(start);
  const endDate = end ? new Date(end) : new Date();
  const diffMs = endDate.getTime() - startDate.getTime();
  if (diffMs < 0 || Number.isNaN(diffMs)) return "—";
  const seconds = Math.floor(diffMs / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  if (hours > 0) return `${hours}h ${minutes % 60}m`;
  if (minutes > 0) return `${minutes}m ${seconds % 60}s`;
  return `${seconds}s`;
}

function getDurationMs(
  start?: string | null,
  end?: string | null,
): number | null {
  if (!start || !end) return null;
  const diffMs = new Date(end).getTime() - new Date(start).getTime();
  return diffMs >= 0 ? diffMs : null;
}

// =============================================================================
// File Tree Utilities
// =============================================================================

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

function getFileIcon(name: string) {
  const ext = name.split(".").pop()?.toLowerCase();
  switch (ext) {
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
    case "md":
    case "txt":
      return FileText;
    default:
      return File;
  }
}

function groupTrialsByAgent(trials: Trial[]): AgentGroup[] {
  const map = new Map<string, AgentGroup>();

  for (const trial of trials) {
    const key = `${trial.agent}|${trial.model || ""}`;
    let group = map.get(key);

    if (!group) {
      group = {
        agent: trial.agent,
        model: trial.model,
        trials: [],
        passCount: 0,
        failCount: 0,
        errorCount: 0,
        avgDuration: null,
      };
      map.set(key, group);
    }

    group.trials.push(trial);

    const status = getMatrixStatus(
      trial.status,
      trial.reward,
      trial.error_message,
    );
    if (status === "pass") group.passCount++;
    else if (status === "fail") group.failCount++;
    else if (status === "harness-error") group.errorCount++;
  }

  // Calculate average duration for each group
  for (const group of map.values()) {
    const durations = group.trials
      .map((t) => getDurationMs(t.started_at, t.finished_at))
      .filter((d): d is number => d !== null);
    if (durations.length > 0) {
      group.avgDuration =
        durations.reduce((a, b) => a + b, 0) / durations.length;
    }
  }

  // Sort: oracle first, then alphabetically
  return Array.from(map.values()).sort((a, b) => {
    if (a.agent.toLowerCase() === "oracle") return -1;
    if (b.agent.toLowerCase() === "oracle") return 1;
    return a.agent.localeCompare(b.agent);
  });
}

// =============================================================================
// Sub-Components
// =============================================================================

function TrialStatusIcon({ trial }: { trial: Trial }) {
  const status = getMatrixStatus(
    trial.status,
    trial.reward,
    trial.error_message,
  );
  const config = STATUS_CONFIG[status];

  return (
    <span
      className={cn(
        "inline-flex h-6 w-6 items-center justify-center rounded border text-xs font-bold transition-all",
        config.matrixClass,
      )}
      title={`${config.shortLabel} • ${trial.reward !== null ? `Reward: ${trial.reward}` : "No reward yet"}`}
    >
      {status === "pending" || status === "queued" || status === "running" ? (
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
      ) : status === "harness-error" ? (
        <Ban className="h-3.5 w-3.5" />
      ) : (
        config.symbol
      )}
    </span>
  );
}

// =============================================================================
// Verdict Card Component
// =============================================================================

function VerdictCard({ task }: { task: Task }) {
  if (!task.verdict_status && !task.verdict) return null;

  return (
    <Card
      className={cn(
        "mb-4",
        task.verdict_status === "running" ||
          task.verdict_status === "pending" ||
          task.verdict_status === "queued"
          ? "border-blue-500/30 bg-blue-500/5"
          : task.verdict?.is_good
            ? "border-emerald-500/30 bg-emerald-500/5"
            : task.verdict?.is_good === false
              ? "border-amber-500/30 bg-amber-500/5"
              : task.verdict_status === "failed"
                ? "border-red-500/30 bg-red-500/5"
                : "border-slate-500/30 bg-slate-500/5",
      )}
    >
      <CardHeader className="pb-1 pt-2 px-3">
        <CardTitle className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider flex items-center gap-1.5">
          <Microscope className="h-3 w-3" />
          Task Verdict
        </CardTitle>
      </CardHeader>
      <CardContent className="px-3 pb-2">
        <div className="flex items-start gap-2">
          {task.verdict_status === "running" ||
          task.verdict_status === "pending" ||
          task.verdict_status === "queued" ? (
            <Loader2 className="h-4 w-4 text-blue-500 animate-spin mt-0.5" />
          ) : task.verdict?.is_good ? (
            <CheckCircle2 className="h-4 w-4 text-emerald-500 mt-0.5" />
          ) : task.verdict?.is_good === false ? (
            <AlertTriangle className="h-4 w-4 text-amber-500 mt-0.5" />
          ) : task.verdict_status === "failed" ? (
            <XCircle className="h-4 w-4 text-red-500 mt-0.5" />
          ) : (
            <Microscope className="h-4 w-4 text-slate-500 mt-0.5" />
          )}
          <div className="flex-1 min-w-0">
            <span className="font-semibold text-xs">
              {task.verdict_status === "running" ||
              task.verdict_status === "pending" ||
              task.verdict_status === "queued"
                ? "Computing..."
                : task.verdict_status === "failed"
                  ? "Failed"
                  : task.verdict?.is_good
                    ? "Good"
                    : task.verdict?.is_good === false
                      ? "Needs Review"
                      : "Pending"}
            </span>
            {task.verdict?.confidence && (
              <span className="text-[10px] text-muted-foreground ml-1">
                ({task.verdict.confidence})
              </span>
            )}
            {task.verdict?.primary_issue && (
              <p className="text-[10px] text-muted-foreground mt-0.5 line-clamp-2">
                {task.verdict.primary_issue}
              </p>
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

// =============================================================================
// File Browser Component
// =============================================================================

function FileBrowser({
  taskId,
  apiBaseUrl,
}: {
  taskId: string;
  apiBaseUrl: string;
}) {
  const [fileTree, setFileTree] = useState<TreeNode[]>([]);
  const [expandedDirs, setExpandedDirs] = useState<Set<string>>(new Set());
  const [selectedFile, setSelectedFile] = useState<TreeNode | null>(null);
  const [fileContent, setFileContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fileContentLoading, setFileContentLoading] = useState(false);

  // Fetch file list
  useEffect(() => {
    let cancelled = false;

    async function fetchFiles() {
      setLoading(true);
      setError(null);

      try {
        const res = await fetch(
          `${apiBaseUrl}/tasks/${taskId}/files?recursive=1`,
        );
        if (!res.ok) {
          throw new Error("Failed to fetch files");
        }
        const data = await res.json();
        const files: TaskFile[] = data.files || [];

        if (!cancelled) {
          const tree = buildTreeFromPaths(files);
          setFileTree(tree);

          // Auto-select first file
          const findFirstFile = (nodes: TreeNode[]): TreeNode | null => {
            for (const node of nodes) {
              if (node.type === "file") return node;
              if (node.children) {
                const found = findFirstFile(node.children);
                if (found) return found;
              }
            }
            return null;
          };
          const first = findFirstFile(tree);
          if (first) setSelectedFile(first);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load files");
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
  }, [taskId, apiBaseUrl]);

  // Fetch file content when selected
  useEffect(() => {
    if (!selectedFile || selectedFile.type !== "file") return;

    if (selectedFile.content !== undefined) {
      setFileContent(selectedFile.content);
      return;
    }

    // Capture the selected file reference for the async function
    const file = selectedFile;
    let cancelled = false;

    async function fetchContent() {
      setFileContentLoading(true);

      try {
        // Try presigned URL first
        if (file.url) {
          const res = await fetch(file.url);
          if (res.ok) {
            const content = await res.text();
            if (!cancelled) {
              setFileContent(content);
              file.content = content;
            }
            return;
          }
        }

        // Fallback to API
        const encodedPath = encodeURIComponent(file.path);
        const res = await fetch(
          `${apiBaseUrl}/tasks/${taskId}/files/${encodedPath}`,
        );
        if (res.ok) {
          const data = await res.json();
          if (!cancelled) {
            setFileContent(data.content || "");
            file.content = data.content || "";
          }
        }
      } catch {
        if (!cancelled) {
          setFileContent("Error loading file");
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
  }, [selectedFile, taskId, apiBaseUrl]);

  const toggleDir = (path: string) => {
    setExpandedDirs((prev) => {
      const next = new Set(prev);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  };

  const renderFileTree = (nodes: TreeNode[], depth = 0) => {
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
          <button
            type="button"
            onClick={() => {
              if (node.type === "dir") {
                toggleDir(node.path);
              } else {
                setSelectedFile(node);
              }
            }}
            className={cn(
              "w-full flex items-center gap-1.5 px-2 py-1 text-left text-xs font-mono rounded transition-colors",
              isSelected
                ? "bg-primary/20 text-primary"
                : "hover:bg-muted text-foreground",
            )}
            style={{ paddingLeft: `${depth * 12 + 8}px` }}
          >
            {node.type === "dir" && (
              <ChevronRightIcon
                className={cn(
                  "h-3 w-3 text-muted-foreground transition-transform",
                  isExpanded && "rotate-90",
                )}
              />
            )}
            {node.type === "file" && <span className="w-3" />}
            <Icon
              className={cn(
                "h-3.5 w-3.5 flex-shrink-0",
                node.type === "dir"
                  ? "text-yellow-500"
                  : "text-muted-foreground",
              )}
            />
            <span className="truncate">{node.name}</span>
          </button>
          {node.type === "dir" && isExpanded && node.children && (
            <div>{renderFileTree(node.children, depth + 1)}</div>
          )}
        </div>
      );
    });
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-8">
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="text-center py-8">
        <AlertCircle className="h-6 w-6 text-red-500 mx-auto mb-2" />
        <p className="text-xs text-muted-foreground">{error}</p>
      </div>
    );
  }

  if (fileTree.length === 0) {
    return (
      <div className="text-center py-8">
        <p className="text-xs text-muted-foreground">No files found</p>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      {/* File tree */}
      <div className="flex-shrink-0 max-h-[40%] overflow-y-auto border-b border-border p-2">
        {renderFileTree(fileTree)}
      </div>

      {/* File content */}
      <div className="flex-1 overflow-hidden flex flex-col min-h-0">
        {selectedFile && (
          <div className="px-2 py-1 border-b border-border bg-muted/30 shrink-0">
            <span className="text-[10px] font-mono text-muted-foreground truncate block">
              {selectedFile.path}
            </span>
          </div>
        )}
        <div className="flex-1 overflow-auto">
          {fileContentLoading ? (
            <div className="p-4 space-y-2">
              <Skeleton className="h-3 w-full" />
              <Skeleton className="h-3 w-3/4" />
              <Skeleton className="h-3 w-5/6" />
            </div>
          ) : selectedFile && fileContent !== null ? (
            <pre className="p-3 text-[10px] font-mono whitespace-pre-wrap overflow-x-auto">
              {fileContent}
            </pre>
          ) : (
            <div className="p-4 text-center text-xs text-muted-foreground">
              Select a file to view
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function AgentGroupCard({
  group,
  selectedTrialId,
  onSelectTrial,
}: {
  group: AgentGroup;
  selectedTrialId: string | null;
  onSelectTrial: (trial: Trial) => void;
}) {
  const completedTrials = group.passCount + group.failCount + group.errorCount;
  const passRate =
    completedTrials > 0 ? (group.passCount / completedTrials) * 100 : 0;

  return (
    <Card className="overflow-hidden">
      <CardHeader className="py-3 px-4 bg-muted/30">
        <div className="flex items-center justify-between">
          <div className="min-w-0">
            <CardTitle className="text-sm font-semibold truncate">
              {group.agent}
            </CardTitle>
            <p className="text-xs text-muted-foreground truncate">
              {group.model || "default model"}
            </p>
          </div>
          <div className="flex items-center gap-2 text-xs shrink-0">
            {group.passCount > 0 && (
              <span className="flex items-center gap-1 text-emerald-500">
                <Trophy className="h-3 w-3" />
                {group.passCount}
              </span>
            )}
            {group.failCount > 0 && (
              <span className="flex items-center gap-1 text-red-500">
                <XCircle className="h-3 w-3" />
                {group.failCount}
              </span>
            )}
            {group.errorCount > 0 && (
              <span className="flex items-center gap-1 text-yellow-500">
                <AlertCircle className="h-3 w-3" />
                {group.errorCount}
              </span>
            )}
          </div>
        </div>
        {/* Pass rate bar */}
        {completedTrials > 0 && (
          <div className="mt-2">
            <div className="flex justify-between text-[10px] text-muted-foreground mb-1">
              <span>Pass rate</span>
              <span>{passRate.toFixed(0)}%</span>
            </div>
            <div className="h-1.5 bg-muted rounded-full overflow-hidden">
              <div
                className="h-full bg-emerald-500 transition-all"
                style={{ width: `${passRate}%` }}
              />
            </div>
          </div>
        )}
      </CardHeader>
      <CardContent className="p-2">
        <div className="flex flex-wrap gap-1.5">
          {group.trials.map((trial) => {
            const isSelected = selectedTrialId === trial.id;
            return (
              <button
                key={trial.id}
                onClick={() => onSelectTrial(trial)}
                className={cn(
                  "relative transition-all rounded",
                  isSelected &&
                    "ring-2 ring-primary ring-offset-2 ring-offset-background",
                )}
              >
                <TrialStatusIcon trial={trial} />
                {trial.analysis_status === "running" && (
                  <span className="absolute -top-0.5 -right-0.5 h-2 w-2 rounded-full bg-blue-400 animate-pulse ring-1 ring-background" />
                )}
              </button>
            );
          })}
        </div>
        {group.avgDuration && (
          <p className="text-[10px] text-muted-foreground mt-2 flex items-center gap-1">
            <Clock className="h-3 w-3" />
            Avg:{" "}
            {formatDuration(
              new Date(0).toISOString(),
              new Date(group.avgDuration).toISOString(),
            )}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

// =============================================================================
// Trial Detail View (right panel content)
// =============================================================================

function TrialDetailView({
  trial,
  task: _task,
  agentGroups,
  onNavigate,
  onRetry,
  apiBaseUrl,
}: {
  trial: Trial;
  task: Task;
  agentGroups: AgentGroup[];
  onNavigate: (trial: Trial) => void;
  onRetry: () => void;
  apiBaseUrl: string;
}) {
  const [activeTab, setActiveTab] = useState("summary");
  const [logCategory, setLogCategory] = useState<LogCategory>("agent");
  const [logCategoryInitialized, setLogCategoryInitialized] = useState(false);
  const [showFullError, setShowFullError] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);

  // Fetch structured logs
  const logsSwrKey =
    activeTab === "logs"
      ? `${apiBaseUrl}/trials/${trial.id}/logs/structured`
      : null;

  const {
    data: structuredLogs,
    error: logsSwrError,
    isLoading: logsLoading,
  } = useSWR<StructuredLogs>(logsSwrKey, fetcher, {
    revalidateOnFocus: false,
    revalidateOnReconnect: false,
  });

  const logsError = logsSwrError?.message ?? null;

  // Auto-select first available category
  useEffect(() => {
    if (!structuredLogs || logCategoryInitialized) return;

    if (
      structuredLogs.agent.oracle ||
      structuredLogs.agent.setup ||
      structuredLogs.agent.commands.length > 0
    ) {
      setLogCategory("agent");
    } else if (
      structuredLogs.verifier.stdout ||
      structuredLogs.verifier.stderr
    ) {
      setLogCategory("verifier");
    } else if (structuredLogs.other && structuredLogs.other.length > 0) {
      setLogCategory("other");
    } else if (structuredLogs.exception) {
      setLogCategory("exception");
    }
    setLogCategoryInitialized(true);
  }, [structuredLogs, logCategoryInitialized]);

  // Reset on trial change
  useEffect(() => {
    setActiveTab("summary");
    setLogCategoryInitialized(false);
    setShowFullError(false);
    setRetrying(false);
    setRetryError(null);
  }, [trial.id]);

  const canRetry = trial.status === "failed" || trial.status === "success";

  const handleRetry = async () => {
    if (retrying) return;
    setRetrying(true);
    setRetryError(null);

    try {
      const res = await fetch(`${apiBaseUrl}/trials/${trial.id}/retry`, {
        method: "POST",
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || data.error || "Failed to retry trial");
      }
      onRetry();
    } catch (err) {
      setRetryError(err instanceof Error ? err.message : "Failed to retry");
    } finally {
      setRetrying(false);
    }
  };

  // Find current group and position for navigation
  const currentGroup = agentGroups.find((g) =>
    g.trials.some((t) => t.id === trial.id),
  );
  const currentGroupTrials = currentGroup?.trials ?? [];
  const currentTrialIndex = currentGroupTrials.findIndex(
    (t) => t.id === trial.id,
  );

  const navigatePrev = () => {
    if (currentTrialIndex > 0) {
      onNavigate(currentGroupTrials[currentTrialIndex - 1]);
    }
  };

  const navigateNext = () => {
    if (currentTrialIndex < currentGroupTrials.length - 1) {
      onNavigate(currentGroupTrials[currentTrialIndex + 1]);
    }
  };

  const status = getMatrixStatus(
    trial.status,
    trial.reward,
    trial.error_message,
  );

  const formatTime = (value?: string | null) => {
    if (!value) return "—";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return "—";
    return parsed.toLocaleTimeString([], {
      hour: "numeric",
      minute: "2-digit",
    });
  };

  const formatDate = (value?: string | null) => {
    if (!value) return "—";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return "—";
    return parsed.toLocaleDateString([], {
      month: "short",
      day: "numeric",
      year: "numeric",
    });
  };

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="px-4 py-3 border-b border-border bg-card shrink-0">
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2 min-w-0">
            <span
              className={cn(
                "inline-flex h-7 w-7 items-center justify-center rounded border text-sm font-bold",
                STATUS_CONFIG[status].matrixClass,
              )}
            >
              {status === "pending" ||
              status === "queued" ||
              status === "running" ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : status === "harness-error" ? (
                <Ban className="h-4 w-4" />
              ) : (
                STATUS_CONFIG[status].symbol
              )}
            </span>
            <div className="min-w-0">
              <h3 className="font-semibold text-sm truncate">{trial.agent}</h3>
              <p className="text-xs text-muted-foreground truncate">
                {trial.model || "default"}
              </p>
            </div>
          </div>
          {trial.reward !== null && (
            <div className="text-right shrink-0">
              <p className="text-xs text-muted-foreground">Reward</p>
              <p
                className={cn(
                  "text-lg font-bold font-mono",
                  trial.reward === 1 ? "text-emerald-500" : "text-red-500",
                )}
              >
                {trial.reward}
              </p>
            </div>
          )}
        </div>

        {/* Navigation */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-1">
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              onClick={navigatePrev}
              disabled={currentTrialIndex <= 0}
            >
              <ChevronLeft className="h-4 w-4" />
            </Button>
            <div className="flex gap-1">
              {currentGroupTrials.map((t) => {
                const s = getMatrixStatus(t.status, t.reward, t.error_message);
                const isActive = t.id === trial.id;
                return (
                  <button
                    key={t.id}
                    onClick={() => onNavigate(t)}
                    className={cn(
                      "h-5 w-5 rounded-sm border text-[10px] font-bold flex items-center justify-center transition",
                      STATUS_CONFIG[s].matrixClass,
                      isActive &&
                        "ring-2 ring-primary ring-offset-1 ring-offset-background",
                    )}
                  >
                    {s === "pending" || s === "queued" || s === "running" ? (
                      <Loader2 className="h-3 w-3 animate-spin" />
                    ) : s === "harness-error" ? (
                      <Ban className="h-3 w-3" />
                    ) : (
                      STATUS_CONFIG[s].symbol
                    )}
                  </button>
                );
              })}
            </div>
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              onClick={navigateNext}
              disabled={currentTrialIndex >= currentGroupTrials.length - 1}
            >
              <ChevronRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      </div>

      {/* Metrics Cards */}
      <div className="px-4 py-3 border-b border-border bg-muted/5 shrink-0">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 items-stretch">
          <div className="min-w-0 h-full">
            <TrialMetricsCards trial={trial} />
          </div>
          {canRetry && (
            <div className="w-full h-full">
              <Button
                onClick={handleRetry}
                disabled={retrying}
                variant="outline"
                className="w-full h-full"
              >
                {retrying ? (
                  <>
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                    Retrying...
                  </>
                ) : (
                  <>
                    <RotateCcw className="h-4 w-4 mr-2" />
                    Retry Trial
                  </>
                )}
              </Button>
              {retryError && (
                <p className="text-xs text-red-500 text-center mt-2">
                  {retryError}
                </p>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Tabs */}
      <Tabs
        value={activeTab}
        onValueChange={setActiveTab}
        className="flex-1 flex flex-col overflow-hidden"
      >
        <div className="border-b border-border px-4">
          <TabsList className="h-10 bg-transparent border-0 p-0 gap-0">
            <TabsTrigger
              value="summary"
              className="data-[state=active]:bg-transparent data-[state=active]:border-b-2 data-[state=active]:border-primary rounded-none px-3 text-xs"
            >
              <FileText className="h-3.5 w-3.5 mr-1.5" />
              Summary
            </TabsTrigger>
            <TabsTrigger
              value="logs"
              className="data-[state=active]:bg-transparent data-[state=active]:border-b-2 data-[state=active]:border-primary rounded-none px-3 text-xs"
            >
              <Terminal className="h-3.5 w-3.5 mr-1.5" />
              Logs
            </TabsTrigger>
            <TabsTrigger
              value="trajectory"
              className="data-[state=active]:bg-transparent data-[state=active]:border-b-2 data-[state=active]:border-primary rounded-none px-3 text-xs"
            >
              <Route className="h-3.5 w-3.5 mr-1.5" />
              Trajectory
            </TabsTrigger>
          </TabsList>
        </div>

        <div className="flex-1 overflow-auto">
          {/* Summary Tab */}
          <TabsContent value="summary" className="m-0 p-4 space-y-4">
            {/* Analysis Card */}
            {(trial.analysis_status || trial.analysis) && (
              <Card
                className={
                  trial.analysis_status === "running" ||
                  trial.analysis_status === "pending" ||
                  trial.analysis_status === "queued"
                    ? "border-blue-500/30 bg-blue-500/5"
                    : trial.analysis?.classification?.startsWith("GOOD")
                      ? "border-emerald-500/30 bg-emerald-500/5"
                      : trial.analysis?.classification?.startsWith("BAD")
                        ? "border-amber-500/30 bg-amber-500/5"
                        : "border-slate-500/30 bg-slate-500/5"
                }
              >
                <CardContent className="py-3 px-4">
                  <div className="flex items-start gap-3">
                    {trial.analysis_status === "running" ||
                    trial.analysis_status === "pending" ||
                    trial.analysis_status === "queued" ? (
                      <Microscope className="h-5 w-5 text-blue-500 animate-pulse mt-0.5" />
                    ) : trial.analysis?.classification?.startsWith("GOOD") ? (
                      <CheckCircle2 className="h-5 w-5 text-emerald-500 mt-0.5" />
                    ) : trial.analysis?.classification?.startsWith("BAD") ? (
                      <AlertTriangle className="h-5 w-5 text-amber-500 mt-0.5" />
                    ) : (
                      <XCircle className="h-5 w-5 text-slate-500 mt-0.5" />
                    )}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="font-bold font-mono text-sm">
                          {trial.analysis_status === "running" ||
                          trial.analysis_status === "pending" ||
                          trial.analysis_status === "queued"
                            ? "Analyzing..."
                            : trial.analysis?.classification?.replace(
                                "_",
                                " ",
                              ) || "Analysis"}
                        </span>
                        {trial.analysis?.subtype && (
                          <span className="text-xs text-muted-foreground">
                            · {trial.analysis.subtype}
                          </span>
                        )}
                      </div>
                      {trial.analysis?.root_cause && (
                        <p className="text-xs text-muted-foreground mt-1">
                          {trial.analysis.root_cause}
                        </p>
                      )}
                      {trial.analysis?.recommendation &&
                        trial.analysis.recommendation !== "N/A" && (
                          <p className="text-xs text-muted-foreground/80 mt-1 italic">
                            💡 {trial.analysis.recommendation}
                          </p>
                        )}
                    </div>
                  </div>
                </CardContent>
              </Card>
            )}

            {/* Execution Timeline */}
            {trial.harbor_stage && (
              <Card>
                <CardHeader className="pb-1 pt-2 px-4">
                  <CardTitle className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider flex items-center justify-between">
                    <span>Execution Timeline</span>
                    <HarborStageBadge stage={trial.harbor_stage} />
                  </CardTitle>
                </CardHeader>
                <CardContent className="px-4 pb-2">
                  <HarborStageTimeline
                    currentStage={trial.harbor_stage}
                    status={trial.status}
                    isFailure={
                      trial.status === "failed" || Boolean(trial.error_message)
                    }
                  />
                </CardContent>
              </Card>
            )}

            {/* Error Card */}
            {trial.error_message && (
              <Card className="border-red-500/30 bg-red-500/5">
                <CardContent className="py-3 px-4">
                  <div className="flex items-start gap-2">
                    <AlertCircle className="h-4 w-4 text-red-500 mt-0.5 shrink-0" />
                    <div className="min-w-0 flex-1">
                      <pre className="text-sm font-mono text-red-600 dark:text-red-400 whitespace-pre-wrap break-words">
                        {showFullError
                          ? trial.error_message
                          : trial.error_message.slice(0, 300)}
                        {trial.error_message.length > 300 &&
                          !showFullError &&
                          "..."}
                      </pre>
                      {trial.error_message.length > 300 && (
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => setShowFullError(!showFullError)}
                          className="mt-2 h-auto px-0 text-xs text-red-500/60 hover:text-red-600"
                        >
                          {showFullError ? (
                            <>
                              <ChevronUp className="h-3 w-3 mr-1" />
                              Show less
                            </>
                          ) : (
                            <>
                              <ChevronDown className="h-3 w-3 mr-1" />
                              Show full error
                            </>
                          )}
                        </Button>
                      )}
                    </div>
                  </div>
                </CardContent>
              </Card>
            )}

            {/* Timing */}
            <Card>
              <CardHeader className="pb-2 pt-3 px-4">
                <CardTitle className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider flex items-center gap-1">
                  <Clock className="h-3 w-3" />
                  Timing
                </CardTitle>
              </CardHeader>
              <CardContent className="px-4 pb-3">
                <div className="text-[10px] text-muted-foreground uppercase tracking-wider mb-2">
                  {formatDate(trial.created_at)}
                </div>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
                  <div>
                    <span className="text-muted-foreground block">Created</span>
                    <span className="font-mono">
                      {formatTime(trial.created_at)}
                    </span>
                  </div>
                  <div>
                    <span className="text-muted-foreground block">Started</span>
                    <span className="font-mono">
                      {formatTime(trial.started_at)}
                    </span>
                  </div>
                  <div>
                    <span className="text-muted-foreground block">
                      Finished
                    </span>
                    <span className="font-mono">
                      {formatTime(trial.finished_at)}
                    </span>
                  </div>
                  <div>
                    <span className="text-muted-foreground block">
                      Duration
                    </span>
                    <span className="font-mono">
                      {formatDuration(trial.started_at, trial.finished_at)}
                    </span>
                  </div>
                </div>
              </CardContent>
            </Card>
          </TabsContent>

          {/* Logs Tab */}
          <TabsContent value="logs" className="m-0 h-full p-0">
            <div className="h-full flex">
              {logsLoading ? (
                <div className="flex-1 p-4 space-y-2">
                  <Skeleton className="h-4 w-full" />
                  <Skeleton className="h-4 w-3/4" />
                  <Skeleton className="h-4 w-5/6" />
                </div>
              ) : logsError ? (
                <div className="flex-1 p-4 text-center">
                  <AlertCircle className="h-8 w-8 text-red-500 mx-auto mb-2" />
                  <p className="text-sm text-muted-foreground">{logsError}</p>
                </div>
              ) : (
                <>
                  {/* Log categories sidebar */}
                  <div className="w-32 shrink-0 border-r border-border bg-muted/20 flex flex-col p-2 space-y-1">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setLogCategory("agent")}
                      className={cn(
                        "w-full justify-start gap-2 px-2 py-1.5 text-xs",
                        logCategory === "agent"
                          ? "bg-primary/10 text-primary font-medium"
                          : "text-muted-foreground hover:bg-muted",
                      )}
                    >
                      <Bot className="h-3.5 w-3.5" />
                      Agent
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setLogCategory("verifier")}
                      className={cn(
                        "w-full justify-start gap-2 px-2 py-1.5 text-xs",
                        logCategory === "verifier"
                          ? "bg-primary/10 text-primary font-medium"
                          : "text-muted-foreground hover:bg-muted",
                      )}
                    >
                      <FlaskConical className="h-3.5 w-3.5" />
                      Tests
                    </Button>
                    {structuredLogs?.other &&
                      structuredLogs.other.length > 0 && (
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => setLogCategory("other")}
                          className={cn(
                            "w-full justify-start gap-2 px-2 py-1.5 text-xs",
                            logCategory === "other"
                              ? "bg-primary/10 text-primary font-medium"
                              : "text-muted-foreground hover:bg-muted",
                          )}
                        >
                          <FileText className="h-3.5 w-3.5" />
                          Other
                        </Button>
                      )}
                    {(structuredLogs?.exception || trial.error_message) && (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setLogCategory("exception")}
                        className={cn(
                          "w-full justify-start gap-2 px-2 py-1.5 text-xs",
                          logCategory === "exception"
                            ? "bg-red-500/10 text-red-500 font-medium"
                            : "text-red-500/70 hover:bg-red-500/10",
                        )}
                      >
                        <Flame className="h-3.5 w-3.5" />
                        Error
                      </Button>
                    )}
                  </div>

                  {/* Log content */}
                  <div className="flex-1 overflow-auto p-3">
                    {logCategory === "agent" && (
                      <div className="space-y-3">
                        {structuredLogs?.agent.oracle && (
                          <div>
                            <h4 className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider mb-1.5">
                              Oracle
                            </h4>
                            <pre className="text-xs font-mono bg-muted/50 p-3 rounded overflow-x-auto whitespace-pre-wrap max-h-64 overflow-y-auto">
                              {structuredLogs.agent.oracle}
                            </pre>
                          </div>
                        )}
                        {structuredLogs?.agent.setup && (
                          <div>
                            <h4 className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider mb-1.5">
                              Setup
                            </h4>
                            <pre className="text-xs font-mono bg-muted/50 p-3 rounded overflow-x-auto whitespace-pre-wrap max-h-64 overflow-y-auto">
                              {structuredLogs.agent.setup}
                            </pre>
                          </div>
                        )}
                        {structuredLogs?.agent.commands.map((cmd) => (
                          <div key={cmd.name}>
                            <h4 className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider mb-1.5">
                              {cmd.name}
                            </h4>
                            <pre className="text-xs font-mono bg-muted/50 p-3 rounded overflow-x-auto whitespace-pre-wrap max-h-64 overflow-y-auto">
                              {cmd.content}
                            </pre>
                          </div>
                        ))}
                        {!structuredLogs?.agent.oracle &&
                          !structuredLogs?.agent.setup &&
                          (!structuredLogs?.agent.commands ||
                            structuredLogs.agent.commands.length === 0) && (
                            <div className="text-center py-8 text-muted-foreground text-sm">
                              No agent logs available
                            </div>
                          )}
                      </div>
                    )}

                    {logCategory === "verifier" && (
                      <div className="space-y-3">
                        {structuredLogs?.verifier.stdout && (
                          <div>
                            <h4 className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider mb-1.5">
                              stdout
                            </h4>
                            <pre className="text-xs font-mono bg-muted/50 p-3 rounded overflow-x-auto whitespace-pre-wrap max-h-96 overflow-y-auto">
                              {structuredLogs.verifier.stdout}
                            </pre>
                          </div>
                        )}
                        {structuredLogs?.verifier.stderr && (
                          <div>
                            <h4 className="text-[10px] font-semibold text-red-500/80 uppercase tracking-wider mb-1.5">
                              stderr
                            </h4>
                            <pre className="text-xs font-mono bg-red-500/5 border border-red-500/20 p-3 rounded overflow-x-auto whitespace-pre-wrap max-h-96 overflow-y-auto text-red-600 dark:text-red-400">
                              {structuredLogs.verifier.stderr}
                            </pre>
                          </div>
                        )}
                        {!structuredLogs?.verifier.stdout &&
                          !structuredLogs?.verifier.stderr && (
                            <div className="text-center py-8 text-muted-foreground text-sm">
                              No test output available
                            </div>
                          )}
                      </div>
                    )}

                    {logCategory === "other" && (
                      <div className="space-y-3">
                        {structuredLogs?.other.map((log, idx) => (
                          <div key={idx}>
                            <h4 className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider mb-1.5">
                              {log.name}
                            </h4>
                            <pre className="text-xs font-mono bg-muted/50 p-3 rounded overflow-x-auto whitespace-pre-wrap max-h-64 overflow-y-auto">
                              {log.content}
                            </pre>
                          </div>
                        ))}
                        {(!structuredLogs?.other ||
                          structuredLogs.other.length === 0) && (
                          <div className="text-center py-8 text-muted-foreground text-sm">
                            No other logs available
                          </div>
                        )}
                      </div>
                    )}

                    {logCategory === "exception" && (
                      <pre className="text-xs font-mono bg-red-500/5 border border-red-500/20 p-3 rounded overflow-x-auto whitespace-pre-wrap text-red-600 dark:text-red-400">
                        {structuredLogs?.exception ||
                          trial.error_message ||
                          "No exception details"}
                      </pre>
                    )}
                  </div>
                </>
              )}
            </div>
          </TabsContent>

          {/* Trajectory Tab */}
          <TabsContent
            value="trajectory"
            className="m-0 h-full p-0 overflow-auto"
          >
            <TrajectoryViewer trialId={trial.id} apiBaseUrl={apiBaseUrl} />
          </TabsContent>
        </div>
      </Tabs>
    </div>
  );
}

// =============================================================================
// Main TaskDetailPanel Component
// =============================================================================

interface TaskDetailPanelProps {
  isOpen: boolean;
  onClose: () => void;
  taskId: string | null;
  /** Ordered list of tasks for navigation */
  orderedTasks?: Task[] | null;
  /** Current task index in the ordered list */
  taskIndex?: number | null;
  /** Callback when navigating to a different task */
  onNavigate?: (task: Task, taskIndex: number) => void;
  /** Callback when data should be refreshed */
  onRefresh?: () => void;
  apiBaseUrl?: string;
}

export function TaskDetailPanel({
  isOpen,
  onClose,
  taskId,
  orderedTasks,
  taskIndex,
  onNavigate,
  onRefresh,
  apiBaseUrl = "/api",
}: TaskDetailPanelProps) {
  const [selectedTrial, setSelectedTrial] = useState<Trial | null>(null);
  const [leftPanelTab, setLeftPanelTab] = useState<LeftPanelTab>("trials");

  // Fetch task with trials
  const {
    data: task,
    error: taskError,
    isLoading: taskLoading,
    mutate: mutateTask,
  } = useSWR<Task>(
    isOpen && taskId
      ? `${apiBaseUrl}/tasks/${taskId}?include_trials=true`
      : null,
    fetcher,
    {
      refreshInterval: (data) => {
        if (!data) return 10000;
        const done = data.status === "completed" || data.status === "failed";
        return done ? 0 : 15000;
      },
    },
  );

  // Group trials by agent
  const agentGroups = useMemo(() => {
    if (!task?.trials) return [];
    return groupTrialsByAgent(task.trials);
  }, [task?.trials]);

  // Auto-select first trial if none selected
  useEffect(() => {
    if (!selectedTrial && task?.trials && task.trials.length > 0) {
      setSelectedTrial(task.trials[0]);
    }
  }, [task?.trials, selectedTrial]);

  // Reset state when panel closes or task changes
  useEffect(() => {
    if (!isOpen) {
      setSelectedTrial(null);
      setLeftPanelTab("trials");
    }
  }, [isOpen]);

  useEffect(() => {
    setSelectedTrial(null);
  }, [taskId]);

  const handleRetry = useCallback(() => {
    mutateTask();
    onRefresh?.();
  }, [mutateTask, onRefresh]);

  // Task navigation
  const orderedList = useMemo(() => orderedTasks ?? [], [orderedTasks]);
  const resolvedIndex =
    typeof taskIndex === "number" && taskIndex >= 0
      ? taskIndex
      : orderedList.findIndex((item) => item.id === taskId);
  const hasNavigation =
    Boolean(onNavigate) && orderedList.length > 1 && resolvedIndex >= 0;
  const canGoPrev = hasNavigation && resolvedIndex > 0;
  const canGoNext = hasNavigation && resolvedIndex < orderedList.length - 1;

  const navigateTo = useCallback(
    (nextIndex: number) => {
      if (!onNavigate) return;
      const nextTask = orderedList[nextIndex];
      if (!nextTask) return;
      onNavigate(nextTask, nextIndex);
    },
    [onNavigate, orderedList],
  );

  // Keyboard navigation
  useEffect(() => {
    if (!isOpen || !hasNavigation) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      const isEditable =
        target.tagName === "INPUT" ||
        target.tagName === "TEXTAREA" ||
        target.isContentEditable;
      if (isEditable) return;

      if (event.key === "ArrowUp" && canGoPrev) {
        event.preventDefault();
        navigateTo(resolvedIndex - 1);
      } else if (event.key === "ArrowDown" && canGoNext) {
        event.preventDefault();
        navigateTo(resolvedIndex + 1);
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isOpen, hasNavigation, canGoPrev, canGoNext, resolvedIndex, navigateTo]);

  if (!taskId) return null;

  const trials = task?.trials ?? [];
  const passCount = trials.filter((t) => t.reward === 1).length;
  const failCount = trials.filter((t) => t.reward === 0).length;
  const errorCount = trials.filter(
    (t) => t.status === "failed" || t.error_message,
  ).length;
  const inProgress = task ? task.total - task.completed - task.failed : 0;

  const taskName = task?.name ?? taskId;

  return (
    <>
      <ResizableDrawer
        open={isOpen}
        onOpenChange={(open) => !open && onClose()}
        defaultWidth={900}
        minWidth={600}
        maxWidth={1400}
      >
        {/* Header */}
        <DrawerHeader className="px-4 py-3 border-b border-border shrink-0">
          <div className="flex items-center justify-between gap-4 pr-20">
            <div className="flex items-center gap-3 min-w-0">
              <div className="min-w-0">
                <DrawerTitle className="text-base font-semibold truncate flex items-center gap-2">
                  {taskName}
                  {task && <StatusBadge status={task.status} />}
                </DrawerTitle>
                <DrawerDescription className="text-xs text-muted-foreground truncate not-sr-only">
                  {task?.task_path || taskId}
                </DrawerDescription>
              </div>
            </div>

            <div className="flex items-center gap-3 shrink-0">
              {/* Stats */}
              <div className="flex items-center gap-3 text-sm">
                <div className="text-center">
                  <p className="text-lg font-semibold">{task?.total ?? 0}</p>
                  <p className="text-[10px] text-muted-foreground">Total</p>
                </div>
                <div className="text-center">
                  <p className="text-lg font-semibold text-emerald-500">
                    {passCount}
                  </p>
                  <p className="text-[10px] text-muted-foreground">Pass</p>
                </div>
                <div className="text-center">
                  <p className="text-lg font-semibold text-red-500">
                    {failCount}
                  </p>
                  <p className="text-[10px] text-muted-foreground">Fail</p>
                </div>
                {errorCount > 0 && (
                  <div className="text-center">
                    <p className="text-lg font-semibold text-yellow-500">
                      {errorCount}
                    </p>
                    <p className="text-[10px] text-muted-foreground">Errors</p>
                  </div>
                )}
                {inProgress > 0 && (
                  <div className="text-center">
                    <p className="text-lg font-semibold text-blue-500">
                      {inProgress}
                    </p>
                    <p className="text-[10px] text-muted-foreground">Running</p>
                  </div>
                )}
              </div>
            </div>
          </div>

          {/* Task navigation */}
          {hasNavigation && (
            <div className="flex items-center gap-2 pt-2 text-xs text-muted-foreground">
              <Button
                variant="ghost"
                size="icon"
                onClick={() => navigateTo(resolvedIndex - 1)}
                disabled={!canGoPrev}
                className="h-7 w-7"
              >
                <ChevronUp className="h-4 w-4" />
              </Button>
              <span>
                Task {resolvedIndex + 1} of {orderedList.length}
              </span>
              <Button
                variant="ghost"
                size="icon"
                onClick={() => navigateTo(resolvedIndex + 1)}
                disabled={!canGoNext}
                className="h-7 w-7"
              >
                <ChevronDown className="h-4 w-4" />
              </Button>
            </div>
          )}
        </DrawerHeader>

        {/* Main content */}
        <div className="flex-1 flex min-h-0 overflow-hidden">
          {taskLoading ? (
            <div className="flex-1 flex items-center justify-center">
              <div className="flex items-center gap-2 text-muted-foreground">
                <Loader2 className="h-5 w-5 animate-spin" />
                <span className="text-sm">Loading task...</span>
              </div>
            </div>
          ) : taskError ? (
            <div className="flex-1 flex items-center justify-center p-4">
              <div className="text-center space-y-2">
                <AlertCircle className="h-8 w-8 text-red-500 mx-auto" />
                <p className="text-sm text-muted-foreground">
                  Failed to load task
                </p>
              </div>
            </div>
          ) : (
            <>
              {/* Left Panel - Task Info & Trials/Files */}
              <div className="w-80 shrink-0 border-r border-border flex flex-col bg-muted/5">
                {/* Verdict Card (always visible at top) */}
                {task && (task.verdict_status || task.verdict) && (
                  <div className="p-3 border-b border-border shrink-0">
                    <VerdictCard task={task} />
                  </div>
                )}

                {/* Tabs for Trials / Files */}
                <Tabs
                  value={leftPanelTab}
                  onValueChange={(v) => setLeftPanelTab(v as LeftPanelTab)}
                  className="flex-1 flex flex-col min-h-0"
                >
                  <div className="border-b border-border px-3 shrink-0">
                    <TabsList className="h-9 bg-transparent border-0 p-0 gap-0 w-full">
                      <TabsTrigger
                        value="trials"
                        className="flex-1 data-[state=active]:bg-transparent data-[state=active]:border-b-2 data-[state=active]:border-primary rounded-none px-2 text-xs"
                      >
                        <Layers className="h-3.5 w-3.5 mr-1.5" />
                        Trials ({trials.length})
                      </TabsTrigger>
                      <TabsTrigger
                        value="files"
                        className="flex-1 data-[state=active]:bg-transparent data-[state=active]:border-b-2 data-[state=active]:border-primary rounded-none px-2 text-xs"
                      >
                        <FolderOpen className="h-3.5 w-3.5 mr-1.5" />
                        Files
                      </TabsTrigger>
                    </TabsList>
                  </div>

                  {/* Trials Tab */}
                  <TabsContent
                    value="trials"
                    className="flex-1 overflow-y-auto m-0 p-3 space-y-3"
                  >
                    {agentGroups.length === 0 ? (
                      <div className="text-center py-8 text-muted-foreground text-sm">
                        No trials yet
                      </div>
                    ) : (
                      agentGroups.map((group) => (
                        <AgentGroupCard
                          key={`${group.agent}-${group.model}`}
                          group={group}
                          selectedTrialId={selectedTrial?.id ?? null}
                          onSelectTrial={setSelectedTrial}
                        />
                      ))
                    )}
                  </TabsContent>

                  {/* Files Tab */}
                  <TabsContent
                    value="files"
                    className="flex-1 overflow-hidden m-0"
                  >
                    {task && (
                      <FileBrowser taskId={task.id} apiBaseUrl={apiBaseUrl} />
                    )}
                  </TabsContent>
                </Tabs>
              </div>

              {/* Right Panel - Trial Details */}
              <div className="flex-1 min-w-0 bg-background overflow-hidden">
                {selectedTrial && task ? (
                  <TrialDetailView
                    trial={selectedTrial}
                    task={task}
                    agentGroups={agentGroups}
                    onNavigate={setSelectedTrial}
                    onRetry={handleRetry}
                    apiBaseUrl={apiBaseUrl}
                  />
                ) : (
                  <div className="h-full flex items-center justify-center text-muted-foreground">
                    <div className="text-center">
                      <Layers className="h-12 w-12 mx-auto mb-4 opacity-30" />
                      <p className="text-sm">Select a trial to view details</p>
                    </div>
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </ResizableDrawer>
    </>
  );
}
