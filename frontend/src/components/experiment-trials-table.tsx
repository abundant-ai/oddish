import {
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  useCallback,
  useDeferredValue,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import dynamic from "next/dynamic";
import { useSearchParams } from "next/navigation";
import { useVirtualizer } from "@tanstack/react-virtual";
import type { Task, Trial, AnalysisClassification } from "@/lib/types";
import {
  getMatrixStatus,
  STATUS_CONFIG,
  type MatrixStatus,
} from "@/lib/status-config";
import {
  Loader2,
  Ban,
  Microscope,
  Check,
  AlertTriangle,
  Copy,
  Trash2,
} from "lucide-react";
import { QueueKeyIcon } from "./queue-key-icon";

const PassAtKGraph = dynamic(
  () => import("./pass-at-k-graph").then((mod) => mod.PassAtKGraph),
  {
    ssr: false,
  },
);

const PassAtOneLeaderboard = dynamic(
  () =>
    import("./pass-at-one-leaderboard").then((mod) => mod.PassAtOneLeaderboard),
  {
    ssr: false,
  },
);

export type AgentSummary = {
  agent: string;
  model: string | null;
  queueKey: string | null;
};

type ExperimentTrialsTableProps = {
  tasks: Task[];
  agentSummaries: AgentSummary[];
  isLoading: boolean;
  topControlsLeft?: ReactNode;
  onTaskDelete?: (task: Task) => Promise<void>;
  onRerun?: () => void;
  allowRerun?: boolean;
  readOnly?: boolean;
  onTrialSelect?: (
    trial: Trial,
    task: Task,
    context: {
      orderedTrials: Trial[];
      trialIndex: number;
      trialGroups: Array<{
        agent: string;
        model: string | null;
        trials: Trial[];
      }>;
    },
  ) => void;
  onTaskSelect?: (
    task: Task,
    context: { orderedTasks: Task[]; taskIndex: number },
  ) => void;
};

const EMPTY_TRIALS: Trial[] = [];
const EMPTY_TRIAL_MAP: ReadonlyMap<string, Trial[]> = new Map<
  string,
  Trial[]
>();
const EMPTY_TRIAL_INDEX: ReadonlyMap<string, number> = new Map<
  string,
  number
>();
const VIRTUALIZATION_THRESHOLD = 50;
const STATUS_FILTER_ORDER: MatrixStatus[] = [
  "pass",
  "fail",
  "harness-error",
  "pending",
  "queued",
  "running",
];

// Analysis classification badge styling
const ANALYSIS_CONFIG: Record<
  AnalysisClassification,
  { label: string; dotClass: string }
> = {
  GOOD_SUCCESS: { label: "Good success", dotClass: "bg-emerald-400" },
  GOOD_FAILURE: { label: "Good failure", dotClass: "bg-slate-400" },
  BAD_SUCCESS: { label: "Bad success", dotClass: "bg-amber-400" },
  BAD_FAILURE: { label: "Bad failure", dotClass: "bg-amber-400" },
  HARNESS_ERROR: { label: "Harness error", dotClass: "bg-slate-500" },
};

function getAnalysisIndicator(trial: Trial): {
  dotClass: string;
  animate: boolean;
  title: string;
} | null {
  const status = trial.analysis_status;
  const analysis = trial.analysis;

  // Analysis in progress - show pulsing indicator
  if (status === "pending" || status === "queued" || status === "running") {
    return {
      dotClass: "bg-blue-400",
      animate: true,
      title: `Analyzing...`,
    };
  }

  // Analysis complete - show classification-based dot
  if (status === "success" && analysis?.classification) {
    const config = ANALYSIS_CONFIG[analysis.classification];
    return {
      dotClass: config.dotClass,
      animate: false,
      title: `${config.label}${analysis.subtype ? `: ${analysis.subtype}` : ""}`,
    };
  }

  // Analysis failed
  if (status === "failed") {
    return {
      dotClass: "bg-red-400",
      animate: false,
      title: "Analysis failed",
    };
  }

  return null;
}

function VerdictIndicator({ task }: { task: Task }) {
  if (!task.run_analysis) return null;

  const status = task.verdict_status;
  const verdict = task.verdict;

  // Still processing
  if (status === "pending" || status === "queued" || status === "running") {
    return (
      <span className="inline-flex items-center ml-1">
        <Microscope className="h-3 w-3 text-muted-foreground animate-pulse" />
      </span>
    );
  }

  // Verdict available
  if (status === "success" && verdict) {
    return (
      <span className="inline-flex items-center ml-1">
        {verdict.is_good ? (
          <Check className="h-3 w-3 text-emerald-500" />
        ) : (
          <AlertTriangle className="h-3 w-3 text-amber-500" />
        )}
      </span>
    );
  }

  // Analysis failed
  if (status === "failed") {
    return (
      <span className="inline-flex items-center ml-1">
        <Microscope className="h-3 w-3 text-red-400" />
      </span>
    );
  }

  // run_analysis is true but no verdict yet (trials still running)
  return (
    <span className="inline-flex items-center ml-1">
      <Microscope className="h-3 w-3 text-muted-foreground/50" />
    </span>
  );
}

function groupTrialsByAgent(trials: Trial[] | null | undefined) {
  const grouped = new Map<string, Trial[]>();
  if (!trials) return grouped;
  for (const trial of trials) {
    const existing = grouped.get(trial.agent) ?? [];
    existing.push(trial);
    grouped.set(trial.agent, existing);
  }
  return grouped;
}

function getTrialTitle(trial: Trial, status: MatrixStatus) {
  const reward =
    trial.reward === null
      ? "reward pending"
      : trial.reward === 1
        ? "reward 1"
        : "reward 0";
  const error = trial.error_message ? ` • ${trial.error_message}` : "";
  return `${STATUS_CONFIG[status].shortLabel} • ${trial.status} • ${reward}${error}`;
}

export function ExperimentTrialsTable({
  tasks,
  agentSummaries,
  isLoading,
  topControlsLeft,
  onTaskDelete,
  onRerun,
  allowRerun = true,
  readOnly = false,
  onTrialSelect,
  onTaskSelect,
}: ExperimentTrialsTableProps) {
  const searchParams = useSearchParams();
  const TASK_COLUMN_MIN = 140;
  const AGENT_COLUMN_MIN = 140;
  const DEFAULT_AGENT_WIDTH = 180;
  const [taskSearch, setTaskSearch] = useState("");
  const deferredTaskSearch = useDeferredValue(taskSearch);
  const [hiddenAgents, setHiddenAgents] = useState<Set<string>>(new Set());
  const [dimmedStatuses, setDimmedStatuses] = useState<Set<MatrixStatus>>(
    new Set(),
  );
  const [selectedTasks, setSelectedTasks] = useState<Set<string>>(new Set());
  const [copiedTaskId, setCopiedTaskId] = useState<string | null>(null);
  const [copiedTable, setCopiedTable] = useState(false);
  const [showPassAtK, setShowPassAtK] = useState(false);
  const [deleteTargets, setDeleteTargets] = useState<Task[]>([]);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);
  const [isRerunning, setIsRerunning] = useState(false);
  const [rerunError, setRerunError] = useState<string | null>(null);
  const [taskColumnWidth, setTaskColumnWidth] = useState(DEFAULT_AGENT_WIDTH);
  const [agentColumnWidths, setAgentColumnWidths] = useState<
    Record<string, number>
  >({});
  const tableContainerRef = useRef<HTMLDivElement | null>(null);
  const [containerWidth, setContainerWidth] = useState<number | null>(null);
  const resizeRef = useRef<{
    columnKey: "task" | string;
    neighborKey: "task" | string;
    startX: number;
    startWidth: number;
    startNeighborWidth: number;
  } | null>(null);
  const [isResizing, setIsResizing] = useState(false);
  const canDeleteTasks = Boolean(onTaskDelete);
  const canRerun = allowRerun;

  const prevUrlRef = useRef({
    hide: "",
    dim: "",
    taskSearch: "",
  });
  const isFirstFilterSync = useRef(true);

  useEffect(() => {
    const urlHide = searchParams.get("hide") || "";
    const urlDim = searchParams.get("dim") || "";
    const urlTaskSearch = searchParams.get("taskSearch") || "";

    if (urlHide !== prevUrlRef.current.hide) {
      setHiddenAgents(new Set(urlHide.split(",").filter(Boolean)));
      prevUrlRef.current.hide = urlHide;
    }

    if (urlDim !== prevUrlRef.current.dim) {
      const next = new Set(
        urlDim
          .split(",")
          .filter(Boolean)
          .filter(
            (value): value is MatrixStatus =>
              value === "pass" ||
              value === "fail" ||
              value === "harness-error" ||
              value === "pending" ||
              value === "queued" ||
              value === "running",
          ),
      );
      setDimmedStatuses(next);
      prevUrlRef.current.dim = urlDim;
    }

    if (urlTaskSearch !== prevUrlRef.current.taskSearch) {
      setTaskSearch(urlTaskSearch);
      prevUrlRef.current.taskSearch = urlTaskSearch;
    }
  }, [searchParams]);

  useEffect(() => {
    if (selectedTasks.size === 0) {
      setRerunError(null);
    }
  }, [selectedTasks]);

  useEffect(() => {
    if (!tableContainerRef.current) return;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      setContainerWidth(Math.floor(entry.contentRect.width));
    });
    observer.observe(tableContainerRef.current);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    // Skip the first render -- the initial state was just read from URL params
    // above, so writing it back would be a no-op at best and could clobber
    // other params (like task/trial) during the same render cycle.
    if (isFirstFilterSync.current) {
      isFirstFilterSync.current = false;
      return;
    }

    const timeoutId = window.setTimeout(() => {
      const params = new URLSearchParams(searchParams.toString());
      const hidden = Array.from(hiddenAgents).sort();
      const dimmed = Array.from(dimmedStatuses).sort();

      if (hidden.length > 0) {
        params.set("hide", hidden.join(","));
      } else {
        params.delete("hide");
      }

      if (dimmed.length > 0) {
        params.set("dim", dimmed.join(","));
      } else {
        params.delete("dim");
      }

      if (deferredTaskSearch.trim()) {
        params.set("taskSearch", deferredTaskSearch.trim());
      } else {
        params.delete("taskSearch");
      }

      const nextQuery = params.toString();
      const currentQuery = searchParams.toString();
      if (nextQuery === currentQuery) return;

      const newUrl = `${window.location.pathname}${nextQuery ? `?${nextQuery}` : ""}`;
      // Keep filter query params in sync without router navigation work.
      window.history.replaceState(window.history.state, "", newUrl);
    }, 250);

    return () => {
      window.clearTimeout(timeoutId);
    };
  }, [hiddenAgents, dimmedStatuses, deferredTaskSearch, searchParams]);

  const sortedAgentSummaries = useMemo(() => {
    const getAgentSortKey = (agentName: string): number => {
      const lower = agentName.toLowerCase();
      if (lower === "nop") return 0;
      if (lower === "oracle") return 1;
      if (lower.startsWith("claude")) return 2;
      if (lower.startsWith("codex")) return 3;
      if (lower.startsWith("gemini")) return 4;
      return 5;
    };

    return [...agentSummaries].sort((a, b) => {
      const keyA = getAgentSortKey(a.agent);
      const keyB = getAgentSortKey(b.agent);
      if (keyA !== keyB) return keyA - keyB;
      return a.agent.localeCompare(b.agent);
    });
  }, [agentSummaries]);

  const visibleAgents = useMemo(
    () =>
      sortedAgentSummaries.filter((agent) => !hiddenAgents.has(agent.agent)),
    [sortedAgentSummaries, hiddenAgents],
  );
  const visibleAgentNames = useMemo(
    () => visibleAgents.map((agent) => agent.agent),
    [visibleAgents],
  );

  const columnOrder = useMemo(
    () => ["task", ...visibleAgents.map((agent) => agent.agent)],
    [visibleAgents],
  );

  const baseTableWidth = useMemo(() => {
    const agentTotal = visibleAgents.reduce(
      (sum, agent) =>
        sum + (agentColumnWidths[agent.agent] ?? DEFAULT_AGENT_WIDTH),
      0,
    );
    return taskColumnWidth + agentTotal;
  }, [visibleAgents, agentColumnWidths, taskColumnWidth, DEFAULT_AGENT_WIDTH]);

  const extraSpace =
    containerWidth !== null ? Math.max(0, containerWidth - baseTableWidth) : 0;
  const columnCount = Math.max(1, columnOrder.length);
  const extraPerColumn = extraSpace / columnCount;
  const tableWidth =
    containerWidth !== null
      ? Math.max(containerWidth, baseTableWidth)
      : baseTableWidth;
  const getDisplayedWidth = (key: "task" | string) => {
    const baseWidth =
      key === "task"
        ? taskColumnWidth
        : (agentColumnWidths[key] ?? DEFAULT_AGENT_WIDTH);
    return baseWidth + extraPerColumn;
  };

  useEffect(() => {
    setAgentColumnWidths((prev) => {
      const next: Record<string, number> = { ...prev };
      let hasChange = false;
      for (const agent of visibleAgents) {
        if (next[agent.agent] == null) {
          next[agent.agent] = DEFAULT_AGENT_WIDTH;
          hasChange = true;
        }
      }
      return hasChange ? next : prev;
    });
  }, [visibleAgents]);

  const filteredTasks = useMemo(() => {
    if (!deferredTaskSearch.trim()) return tasks;
    const query = deferredTaskSearch.trim().toLowerCase();
    return tasks.filter((task) => {
      const haystack = [task.name, task.task_path, task.id]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return haystack.includes(query);
    });
  }, [tasks, deferredTaskSearch]);

  const getTaskContext = useMemo(() => {
    const contextCache = new Map<
      string,
      {
        groupedTrialsByAgent: Map<string, Trial[]>;
        orderedTrials: Trial[];
        trialIndexById: Map<string, number>;
        trialGroups: Array<{
          agent: string;
          model: string | null;
          trials: Trial[];
        }>;
      }
    >();

    return (task: Task) => {
      const cached = contextCache.get(task.id);
      if (cached) return cached;

      const groupedTrialsByAgent = groupTrialsByAgent(task.trials);
      const orderedTrials: Trial[] = [];
      const trialIndexById = new Map<string, number>();
      const trialGroups: Array<{
        agent: string;
        model: string | null;
        trials: Trial[];
      }> = [];

      for (const agentName of visibleAgentNames) {
        const trials = groupedTrialsByAgent.get(agentName) ?? EMPTY_TRIALS;
        if (trials.length > 0) {
          const model = trials.find((trial) => trial.model)?.model ?? null;
          trialGroups.push({ agent: agentName, model, trials });
        }
        for (const trial of trials) {
          trialIndexById.set(trial.id, orderedTrials.length);
          orderedTrials.push(trial);
        }
      }

      const context = {
        groupedTrialsByAgent,
        orderedTrials,
        trialIndexById,
        trialGroups,
      };
      contextCache.set(task.id, context);
      return context;
    };
  }, [visibleAgentNames]);

  const selectedTaskList = useMemo(
    () => tasks.filter((task) => selectedTasks.has(task.id)),
    [tasks, selectedTasks],
  );

  const selectedRetryableTrials = useMemo(() => {
    const seen = new Set<string>();
    const retryable: Trial[] = [];
    for (const task of selectedTaskList) {
      for (const trial of task.trials ?? []) {
        if (
          (trial.status === "failed" || trial.status === "success") &&
          !seen.has(trial.id)
        ) {
          seen.add(trial.id);
          retryable.push(trial);
        }
      }
    }
    return retryable;
  }, [selectedTaskList]);

  const rowVirtualizer = useVirtualizer({
    count: filteredTasks.length,
    getScrollElement: () => tableContainerRef.current,
    estimateSize: () => 46,
    overscan: 4,
  });

  const shouldVirtualize = filteredTasks.length >= VIRTUALIZATION_THRESHOLD;
  const virtualRows = shouldVirtualize ? rowVirtualizer.getVirtualItems() : [];
  const rowsToRender = shouldVirtualize
    ? virtualRows.map((virtualRow) => ({
        task: filteredTasks[virtualRow.index],
        index: virtualRow.index,
        virtualRow,
      }))
    : filteredTasks.map((task, index) => ({ task, index, virtualRow: null }));
  const paddingTop = virtualRows.length > 0 ? virtualRows[0].start : 0;
  const paddingBottom =
    virtualRows.length > 0
      ? rowVirtualizer.getTotalSize() - virtualRows[virtualRows.length - 1].end
      : 0;

  const toggleStatus = (status: MatrixStatus) => {
    setDimmedStatuses((prev) => {
      const next = new Set(prev);
      if (next.has(status)) {
        next.delete(status);
      } else {
        next.add(status);
      }
      return next;
    });
  };

  const toggleAgent = useCallback((agentName: string) => {
    setHiddenAgents((prev) => {
      const next = new Set(prev);
      if (next.has(agentName)) {
        next.delete(agentName);
      } else {
        next.add(agentName);
      }
      return next;
    });
  }, []);

  const handleTaskSearchChange = (value: string) => {
    setTaskSearch(value);
  };

  const handleCopyTaskName = async (taskId: string, taskName: string) => {
    await navigator.clipboard.writeText(taskName);
    setCopiedTaskId(taskId);
    setTimeout(() => {
      setCopiedTaskId((prev) => (prev === taskId ? null : prev));
    }, 2000);
  };

  const handleCopyTableAsTSV = async () => {
    // Generate TSV header
    const headers = ["Task", ...visibleAgents.map((agent) => agent.agent)];
    const rows: string[] = [headers.join("\t")];

    // Generate TSV rows
    for (const task of filteredTasks) {
      const grouped =
        getTaskContext(task).groupedTrialsByAgent ?? EMPTY_TRIAL_MAP;

      const rowCells = [task.name];
      for (const agent of visibleAgents) {
        const trials = grouped.get(agent.agent) ?? [];
        if (trials.length === 0) {
          rowCells.push("—");
        } else {
          // Show status for each trial, comma-separated
          const statuses = trials.map((trial) => {
            const status = getMatrixStatus(
              trial.status,
              trial.reward,
              trial.error_message,
            );
            return STATUS_CONFIG[status].shortLabel;
          });
          rowCells.push(statuses.join(", "));
        }
      }
      rows.push(rowCells.join("\t"));
    }

    const tsv = rows.join("\n");
    await navigator.clipboard.writeText(tsv);
    setCopiedTable(true);
    setTimeout(() => {
      setCopiedTable(false);
    }, 2000);
  };

  const deleteTargetSummary = useMemo(() => {
    if (deleteTargets.length === 0) {
      return { label: "", taskCount: 0, trialCount: 0 };
    }
    if (deleteTargets.length === 1) {
      const target = deleteTargets[0];
      return {
        label: target.name,
        taskCount: 1,
        trialCount: target.total ?? 0,
      };
    }
    const trialCount = deleteTargets.reduce(
      (sum, task) => sum + (task.total ?? 0),
      0,
    );
    return {
      label: `${deleteTargets.length} tasks`,
      taskCount: deleteTargets.length,
      trialCount,
    };
  }, [deleteTargets]);

  const handleDeleteTasks = async () => {
    if (deleteTargets.length === 0 || !onTaskDelete || isDeleting) return;
    setIsDeleting(true);
    setDeleteError(null);

    try {
      let firstError: string | null = null;
      const failedTargets: Task[] = [];
      const nextSelected = new Set(selectedTasks);

      for (const target of deleteTargets) {
        try {
          await onTaskDelete(target);
          nextSelected.delete(target.id);
        } catch (error) {
          failedTargets.push(target);
          if (!firstError) {
            firstError =
              error instanceof Error ? error.message : "Failed to delete task";
          }
        }
      }

      setSelectedTasks(nextSelected);
      setDeleteTargets(failedTargets);
      if (firstError) {
        setDeleteError(firstError);
      }
    } catch (error) {
      setDeleteError(
        error instanceof Error ? error.message : "Failed to delete task",
      );
    } finally {
      setIsDeleting(false);
    }
  };

  const handleRerunSelectedTasks = async () => {
    if (!canRerun || isRerunning) return;
    if (selectedRetryableTrials.length === 0) {
      setRerunError("No retryable trials in selection.");
      return;
    }

    setIsRerunning(true);
    setRerunError(null);

    try {
      const results = await Promise.allSettled(
        selectedRetryableTrials.map(async (trial) => {
          const res = await fetch(`/api/trials/${trial.id}/retry`, {
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
      onRerun?.();
    } finally {
      setIsRerunning(false);
    }
  };

  const startResize = (
    event: ReactMouseEvent,
    columnKey: "task" | string,
    startWidth: number,
  ) => {
    event.preventDefault();
    const currentIndex = columnOrder.indexOf(columnKey);
    if (currentIndex === -1) return;
    const neighborIndex =
      currentIndex < columnOrder.length - 1
        ? currentIndex + 1
        : currentIndex - 1;
    const neighborKey = columnOrder[neighborIndex];
    if (!neighborKey) return;

    const getColumnWidth = (key: string) =>
      key === "task"
        ? taskColumnWidth
        : (agentColumnWidths[key] ?? DEFAULT_AGENT_WIDTH);

    resizeRef.current = {
      columnKey,
      neighborKey,
      startX: event.clientX,
      startWidth,
      startNeighborWidth: getColumnWidth(neighborKey),
    };
    setIsResizing(true);
  };

  useEffect(() => {
    if (!isResizing) return;

    const handleMouseMove = (event: MouseEvent) => {
      if (!resizeRef.current) return;
      const deltaX = event.clientX - resizeRef.current.startX;
      const targetKey = resizeRef.current.columnKey;
      const neighborKey = resizeRef.current.neighborKey;
      const targetMin =
        targetKey === "task" ? TASK_COLUMN_MIN : AGENT_COLUMN_MIN;
      const neighborMin =
        neighborKey === "task" ? TASK_COLUMN_MIN : AGENT_COLUMN_MIN;

      let nextTargetWidth = resizeRef.current.startWidth + deltaX;
      let nextNeighborWidth = resizeRef.current.startNeighborWidth - deltaX;

      if (nextTargetWidth < targetMin) {
        const clampedDelta = targetMin - resizeRef.current.startWidth;
        nextTargetWidth = targetMin;
        nextNeighborWidth = resizeRef.current.startNeighborWidth - clampedDelta;
      }

      if (nextNeighborWidth < neighborMin) {
        const clampedDelta = resizeRef.current.startNeighborWidth - neighborMin;
        nextNeighborWidth = neighborMin;
        nextTargetWidth = resizeRef.current.startWidth + clampedDelta;
      }

      if (targetKey === "task" && neighborKey === "task") {
        setTaskColumnWidth(nextTargetWidth);
        return;
      }

      if (targetKey === "task") {
        setTaskColumnWidth(nextTargetWidth);
        setAgentColumnWidths((prev) => ({
          ...prev,
          [neighborKey]: nextNeighborWidth,
        }));
        return;
      }

      if (neighborKey === "task") {
        setTaskColumnWidth(nextNeighborWidth);
        setAgentColumnWidths((prev) => ({
          ...prev,
          [targetKey]: nextTargetWidth,
        }));
        return;
      }

      setAgentColumnWidths((prev) => ({
        ...prev,
        [targetKey]: nextTargetWidth,
        [neighborKey]: nextNeighborWidth,
      }));
    };

    const handleMouseUp = () => {
      resizeRef.current = null;
      setIsResizing(false);
    };

    window.addEventListener("mousemove", handleMouseMove);
    window.addEventListener("mouseup", handleMouseUp);

    return () => {
      window.removeEventListener("mousemove", handleMouseMove);
      window.removeEventListener("mouseup", handleMouseUp);
    };
  }, [isResizing]);

  const renderStatusFilters = () => (
    <div className="flex flex-wrap items-center gap-2">
      {STATUS_FILTER_ORDER.map((status) => {
        const config = STATUS_CONFIG[status];
        const isDimmed = dimmedStatuses.has(status);
        return (
          <Tooltip key={status}>
            <TooltipTrigger asChild>
              <Button
                type="button"
                onClick={() => toggleStatus(status)}
                variant="ghost"
                size="sm"
                className={`h-auto flex items-center gap-1 rounded border px-2 py-1 text-[10px] font-semibold transition ${
                  isDimmed
                    ? "border-border text-muted-foreground line-through"
                    : "border-transparent hover:border-border"
                }`}
              >
                <span
                  className={`inline-flex h-4 w-4 items-center justify-center rounded-sm text-[10px] ${config.matrixClass}`}
                >
                  {status === "pending" ||
                  status === "queued" ||
                  status === "running" ? (
                    <Loader2 className="h-3 w-3" />
                  ) : status === "harness-error" ? (
                    <Ban className="h-3 w-3" />
                  ) : (
                    config.symbol
                  )}
                </span>
                <span className="uppercase tracking-wide">
                  {config.shortLabel}
                </span>
              </Button>
            </TooltipTrigger>
            <TooltipContent>
              {config.shortLabel} ({isDimmed ? "dimmed" : "visible"})
            </TooltipContent>
          </Tooltip>
        );
      })}
    </div>
  );

  const renderAgentFilterMenu = () => (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-auto select-none px-2 py-1 text-[10px] font-semibold uppercase tracking-wide"
        >
          Filter agents ({visibleAgents.length}/{sortedAgentSummaries.length})
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-64 max-h-64 overflow-auto p-2">
        <div className="flex items-center justify-between px-1 pb-2 text-[10px] text-muted-foreground">
          <span>Show/hide agent columns</span>
          <Button
            type="button"
            variant="link"
            size="sm"
            onClick={() => {
              const next = new Set<string>();
              setHiddenAgents(next);
            }}
            className="h-auto p-0 text-[10px]"
          >
            Show all
          </Button>
        </div>
        <div className="space-y-1">
          {sortedAgentSummaries.map((agent) => {
            const isVisible = !hiddenAgents.has(agent.agent);
            return (
              <label
                key={agent.agent}
                className={`flex items-center gap-2 rounded px-2 py-1 text-xs ${
                  isVisible ? "hover:bg-muted" : "text-muted-foreground"
                }`}
              >
                <Checkbox
                  checked={isVisible}
                  onCheckedChange={() => toggleAgent(agent.agent)}
                  className="h-3.5 w-3.5"
                />
                <span className={`${isVisible ? "" : "line-through"}`}>
                  {agent.agent}
                </span>
                <span className="text-[10px] text-muted-foreground flex items-center gap-1">
                  <QueueKeyIcon
                    queueKey={agent.queueKey}
                    model={agent.model}
                    agent={agent.agent}
                    size={10}
                    className="shrink-0"
                  />
                  {agent.model ?? "—"}
                </span>
              </label>
            );
          })}
        </div>
      </PopoverContent>
    </Popover>
  );

  const selectAllVisible = () => {
    setSelectedTasks(new Set(filteredTasks.map((task) => task.id)));
  };

  const clearSelection = () => {
    setSelectedTasks(new Set());
  };

  return (
    <TooltipProvider>
      <div className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">{topControlsLeft}</div>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => setShowPassAtK((prev) => !prev)}
            className="h-auto px-2 py-1 text-[10px] font-semibold uppercase tracking-wide"
          >
            {showPassAtK ? "Hide graph" : "Show graph"}
          </Button>
        </div>
        {/* Pass@k Graph - only shows when there are multiple trials per task-agent */}
        {showPassAtK ? (
          <div className="grid gap-4 xl:grid-cols-2 items-stretch">
            <div className="min-w-0 h-full">
              <PassAtKGraph
                tasks={tasks}
                agentSummaries={sortedAgentSummaries}
                hiddenAgents={hiddenAgents}
                onToggleAgent={toggleAgent}
              />
            </div>
            <div className="min-w-0 h-full">
              <PassAtOneLeaderboard
                tasks={tasks}
                agentSummaries={sortedAgentSummaries}
                hiddenAgents={hiddenAgents}
                onToggleAgent={toggleAgent}
              />
            </div>
          </div>
        ) : null}

        <div className="rounded-lg border border-border bg-card shadow-sm max-w-full overflow-hidden">
          <div className="border-b border-border bg-card/70 px-3 py-2 space-y-2 relative z-30">
            <div className="flex flex-wrap items-center gap-3">
              <div className="flex-1 min-w-[200px] max-w-[420px]">
                <Input
                  type="search"
                  value={taskSearch}
                  onChange={(event) =>
                    handleTaskSearchChange(event.target.value)
                  }
                  placeholder="Search tasks"
                  className="h-9 text-xs"
                />
              </div>
              {renderStatusFilters()}
              {renderAgentFilterMenu()}
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={handleCopyTableAsTSV}
                    className="h-auto select-none px-2 py-1 text-[10px] font-semibold uppercase tracking-wide"
                  >
                    {copiedTable ? (
                      <>
                        <Check className="h-3 w-3 mr-1 text-emerald-500" />
                        Copied
                      </>
                    ) : (
                      <>
                        <Copy className="h-3 w-3 mr-1" />
                        Copy TSV
                      </>
                    )}
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Copy table as TSV</TooltipContent>
              </Tooltip>
            </div>
            <div
              className={`flex flex-wrap items-center gap-2 text-[10px] text-muted-foreground ${!readOnly && selectedTasks.size > 0 ? "" : "hidden"}`}
            >
              <span>{selectedTasks.size} selected</span>
              <Button
                type="button"
                variant="link"
                size="sm"
                onClick={clearSelection}
                className="h-auto p-0 text-[10px]"
              >
                Clear
              </Button>
              {canRerun && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={handleRerunSelectedTasks}
                  disabled={isRerunning || selectedRetryableTrials.length === 0}
                  className="h-auto px-2 py-1 text-[10px] font-semibold uppercase tracking-wide"
                >
                  {isRerunning
                    ? "Rerunning..."
                    : `Rerun trials (${selectedRetryableTrials.length})`}
                </Button>
              )}
              {canDeleteTasks && (
                <Button
                  type="button"
                  variant="destructive"
                  size="sm"
                  onClick={() => {
                    setDeleteTargets(selectedTaskList);
                    setDeleteError(null);
                  }}
                  disabled={isDeleting || selectedTaskList.length === 0}
                  className="h-auto px-2 py-1 text-[10px] font-semibold uppercase tracking-wide"
                >
                  <Trash2 className="h-3 w-3 mr-1" />
                  Delete
                </Button>
              )}
              {rerunError && (
                <span className="text-[10px] text-red-500">{rerunError}</span>
              )}
            </div>
          </div>
          <div
            ref={tableContainerRef}
            className={`overflow-x-auto overflow-y-auto max-h-[70vh] ${isResizing ? "select-none" : ""}`}
          >
            <table
              className="w-full caption-bottom text-sm min-w-[960px]"
              style={{ tableLayout: "fixed", width: tableWidth }}
            >
              <colgroup>
                <col style={{ width: `${getDisplayedWidth("task")}px` }} />
                {visibleAgents.map((agent) => (
                  <col
                    key={`col-${agent.agent}`}
                    style={{
                      width: `${getDisplayedWidth(agent.agent)}px`,
                    }}
                  />
                ))}
              </colgroup>
              <TableHeader className="sticky top-0 z-20 bg-muted">
                <TableRow className="hover:bg-transparent border-b-2 border-border">
                  <TableHead
                    className="sticky left-0 z-30 bg-muted font-mono font-bold text-foreground border-r border-border shadow-[2px_0_5px_-2px_rgba(0,0,0,0.1)] relative"
                    style={{ width: getDisplayedWidth("task") }}
                  >
                    <div className="flex items-center gap-2">
                      <span className="text-[10px] text-muted-foreground w-5 text-right flex-shrink-0">
                        #
                      </span>
                      {!readOnly && (
                        <Checkbox
                          checked={
                            filteredTasks.length > 0 &&
                            selectedTasks.size === filteredTasks.length
                          }
                          onCheckedChange={(checked) => {
                            if (checked) {
                              selectAllVisible();
                            } else {
                              clearSelection();
                            }
                          }}
                          className="h-4 w-4"
                        />
                      )}
                      <span className="text-xs sm:text-sm">Task</span>
                    </div>
                    <div
                      className="absolute right-0 top-0 h-full w-1.5 cursor-col-resize"
                      onMouseDown={(event) =>
                        startResize(event, "task", taskColumnWidth)
                      }
                    />
                  </TableHead>
                  {visibleAgents.map((agent, agentIndex) => (
                    <TableHead
                      key={agent.agent}
                      className="text-center font-mono border-r border-border last:border-r-0 px-1 sm:px-2 bg-muted relative"
                      style={{
                        width: getDisplayedWidth(agent.agent),
                      }}
                    >
                      <div className="flex flex-col gap-0.5 items-center min-w-[60px] sm:min-w-[80px] md:min-w-[100px]">
                        <div className="text-[10px] sm:text-xs font-bold text-foreground truncate max-w-[70px] sm:max-w-[110px] md:max-w-none">
                          {agent.agent}
                        </div>
                        <div className="text-[9px] sm:text-[10px] font-normal text-muted-foreground truncate max-w-[70px] sm:max-w-[110px] md:max-w-none flex items-center justify-center gap-1">
                          <QueueKeyIcon
                            queueKey={agent.queueKey}
                            model={agent.model}
                            agent={agent.agent}
                            size={11}
                            className="shrink-0"
                          />
                          {agent.model ?? "—"}
                        </div>
                      </div>
                      {agentIndex < visibleAgents.length - 1 && (
                        <div
                          className="absolute right-0 top-0 h-full w-1.5 cursor-col-resize"
                          onMouseDown={(event) =>
                            startResize(
                              event,
                              agent.agent,
                              agentColumnWidths[agent.agent] ??
                                DEFAULT_AGENT_WIDTH,
                            )
                          }
                        />
                      )}
                    </TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {shouldVirtualize && paddingTop > 0 && (
                  <TableRow aria-hidden>
                    <TableCell
                      colSpan={Math.max(1, visibleAgents.length + 1)}
                      style={{
                        height: `${paddingTop}px`,
                        padding: 0,
                        border: 0,
                      }}
                    />
                  </TableRow>
                )}
                {rowsToRender.map((row) => {
                  const task = row.task;
                  const index = row.index;
                  if (!task) return null;
                  const context = getTaskContext(task);
                  const grouped =
                    context?.groupedTrialsByAgent ?? EMPTY_TRIAL_MAP;
                  const orderedTrials = context?.orderedTrials ?? EMPTY_TRIALS;
                  const trialIndexById =
                    context?.trialIndexById ?? EMPTY_TRIAL_INDEX;
                  const trialGroups = context?.trialGroups ?? [];
                  return (
                    <TableRow
                      key={task.id}
                      data-index={index}
                      ref={(node) => {
                        if (node && row.virtualRow) {
                          rowVirtualizer.measureElement(node);
                        }
                      }}
                      className={
                        index % 2 === 0
                          ? "bg-background hover:bg-muted/30"
                          : "bg-muted/20 hover:bg-muted/40"
                      }
                    >
                      <TableCell
                        className={`sticky left-0 z-10 font-mono text-xs border-r border-border shadow-[2px_0_5px_-2px_rgba(0,0,0,0.1)] ${index % 2 === 0 ? "bg-background" : "bg-muted/20"}`}
                        style={{ width: getDisplayedWidth("task") }}
                      >
                        <div className="flex items-center gap-2">
                          <span className="text-[10px] text-muted-foreground w-5 text-right flex-shrink-0">
                            {index + 1}
                          </span>
                          {!readOnly && (
                            <Checkbox
                              checked={selectedTasks.has(task.id)}
                              onCheckedChange={() => {
                                setSelectedTasks((prev) => {
                                  const next = new Set(prev);
                                  if (next.has(task.id)) {
                                    next.delete(task.id);
                                  } else {
                                    next.add(task.id);
                                  }
                                  return next;
                                });
                              }}
                              className="h-4 w-4"
                            />
                          )}
                          <div className="flex flex-col">
                            <div className="flex items-center gap-1">
                              <Tooltip>
                                <TooltipTrigger asChild>
                                  <button
                                    type="button"
                                    onClick={() =>
                                      onTaskSelect?.(task, {
                                        orderedTasks: filteredTasks,
                                        taskIndex: index,
                                      })
                                    }
                                    className="font-medium text-foreground truncate text-left hover:text-blue-400 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 cursor-pointer"
                                  >
                                    {task.name}
                                  </button>
                                </TooltipTrigger>
                                <TooltipContent>View task files</TooltipContent>
                              </Tooltip>
                              <VerdictIndicator task={task} />
                              <Tooltip>
                                <TooltipTrigger asChild>
                                  <Button
                                    type="button"
                                    variant="ghost"
                                    size="icon"
                                    onClick={() =>
                                      handleCopyTaskName(task.id, task.name)
                                    }
                                    className="h-5 w-5 text-muted-foreground hover:text-foreground"
                                    aria-label="Copy task name"
                                  >
                                    {copiedTaskId === task.id ? (
                                      <Check className="h-3.5 w-3.5 text-emerald-500" />
                                    ) : (
                                      <Copy className="h-3.5 w-3.5" />
                                    )}
                                  </Button>
                                </TooltipTrigger>
                                <TooltipContent>Copy task name</TooltipContent>
                              </Tooltip>
                            </div>
                          </div>
                        </div>
                      </TableCell>
                      {visibleAgents.map((agent) => {
                        const trials = grouped.get(agent.agent) ?? EMPTY_TRIALS;
                        return (
                          <TableCell
                            key={`${task.id}-${agent.agent}`}
                            className="text-center border-r border-border last:border-r-0"
                            style={{
                              width: getDisplayedWidth(agent.agent),
                            }}
                          >
                            {trials.length === 0 ? (
                              <span className="text-xs text-muted-foreground">
                                —
                              </span>
                            ) : (
                              <div className="flex flex-wrap justify-center gap-1">
                                {trials.map((trial, trialIndex) => {
                                  const status = getMatrixStatus(
                                    trial.status,
                                    trial.reward,
                                    trial.error_message,
                                  );
                                  const config = STATUS_CONFIG[status];
                                  const isDimmed = dimmedStatuses.has(status);
                                  // Keep harness errors visually prominent even when dim-filtered.
                                  const dimClass =
                                    isDimmed && status !== "harness-error"
                                      ? "opacity-25"
                                      : "";
                                  const analysisIndicator =
                                    getAnalysisIndicator(trial);
                                  // Build enhanced title with analysis info
                                  const baseTitle = getTrialTitle(
                                    trial,
                                    status,
                                  );
                                  const analysisTitle = analysisIndicator
                                    ? ` • ${analysisIndicator.title}`
                                    : "";
                                  const fullTitle = `${baseTitle}${analysisTitle}`;
                                  return (
                                    <div key={trial.id} className="relative">
                                      <button
                                        type="button"
                                        onClick={() => {
                                          const trialIndex =
                                            trialIndexById.get(trial.id) ?? 0;
                                          onTrialSelect?.(trial, task, {
                                            orderedTrials,
                                            trialIndex,
                                            trialGroups,
                                          });
                                        }}
                                        className={`h-5 w-5 rounded-sm border p-0 text-sm font-semibold leading-none transition hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 cursor-pointer flex items-center justify-center ${config.matrixClass} ${dimClass}`}
                                        aria-label={`Trial ${trialIndex + 1} ${config.shortLabel}`}
                                        title={fullTitle}
                                      >
                                        {status === "pending" ||
                                        status === "queued" ||
                                        status === "running" ? (
                                          <Loader2 className="h-3.5 w-3.5" />
                                        ) : status === "harness-error" ? (
                                          <Ban className="h-3.5 w-3.5" />
                                        ) : (
                                          config.symbol
                                        )}
                                      </button>
                                      {analysisIndicator && (
                                        <span
                                          className={`absolute -top-0.5 -right-0.5 h-1.5 w-1.5 rounded-full ring-1 ring-background ${analysisIndicator.dotClass} ${analysisIndicator.animate ? "animate-pulse" : ""}`}
                                        />
                                      )}
                                    </div>
                                  );
                                })}
                              </div>
                            )}
                          </TableCell>
                        );
                      })}
                    </TableRow>
                  );
                })}
                {shouldVirtualize && paddingBottom > 0 && (
                  <TableRow aria-hidden>
                    <TableCell
                      colSpan={Math.max(1, visibleAgents.length + 1)}
                      style={{
                        height: `${paddingBottom}px`,
                        padding: 0,
                        border: 0,
                      }}
                    />
                  </TableRow>
                )}
                {filteredTasks.length === 0 && !isLoading && (
                  <TableRow>
                    <TableCell
                      colSpan={Math.max(1, visibleAgents.length + 1)}
                      className="text-center text-muted-foreground py-8"
                    >
                      No tasks found for this experiment
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </table>
          </div>
        </div>
      </div>
      {canDeleteTasks && (
        <AlertDialog
          open={deleteTargets.length > 0}
          onOpenChange={(open) => {
            if (!open && !isDeleting) {
              setDeleteTargets([]);
              setDeleteError(null);
            }
          }}
        >
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>
                {deleteTargetSummary.taskCount > 1
                  ? "Delete selected tasks?"
                  : "Delete this task?"}
              </AlertDialogTitle>
              <AlertDialogDescription>
                This permanently deletes{" "}
                <span className="font-medium text-foreground">
                  {deleteTargetSummary.label}
                </span>{" "}
                and removes {deleteTargetSummary.trialCount} trials. This action
                cannot be undone.
              </AlertDialogDescription>
            </AlertDialogHeader>
            {deleteError && (
              <Alert variant="destructive">
                <AlertTitle>Delete failed</AlertTitle>
                <AlertDescription>{deleteError}</AlertDescription>
              </Alert>
            )}
            <AlertDialogFooter>
              <AlertDialogCancel disabled={isDeleting}>
                Cancel
              </AlertDialogCancel>
              <AlertDialogAction
                onClick={handleDeleteTasks}
                disabled={isDeleting}
                className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              >
                {isDeleting ? "Deleting..." : "Delete task"}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      )}
    </TooltipProvider>
  );
}
