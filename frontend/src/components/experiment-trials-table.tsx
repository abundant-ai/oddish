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
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
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
} from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import dynamic from "next/dynamic";
import { useSearchParams } from "next/navigation";
import { useVirtualizer } from "@tanstack/react-virtual";
import type { Task, Trial, AnalysisClassification } from "@/lib/types";
import {
  getExperimentAgentKey,
  type ExperimentAgentSummary,
} from "@/lib/experiment-agent-grouping";
import {
  isActivePipelineStatus,
  taskHasCancellableWork,
} from "@/lib/job-status";
import {
  formatPartialRewardBadgeValue,
  formatRewardPercent,
  formatRewardValue,
  getMatrixStatus,
  getRewardStyle,
  STATUS_CONFIG,
  STATUS_GLYPH_BOX,
  type MatrixStatus,
} from "@/lib/status-config";
import {
  Loader2,
  Check,
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  ChevronDown,
  Copy,
  OctagonX,
  Search,
  Trash2,
} from "lucide-react";
import { QueueKeyIcon } from "./queue-key-icon";
import { StatusIcon } from "./status-icon";

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

export type AgentSummary = ExperimentAgentSummary;

type ExperimentTrialsTableProps = {
  tasks: Task[];
  agentSummaries: AgentSummary[];
  modelScopedAgents: ReadonlySet<string>;
  isLoading: boolean;
  isLoadingTrials?: boolean;
  showPassAtK?: boolean;
  onTaskDelete?: (task: Task) => Promise<void>;
  onRerun?: (taskIds?: string[]) => void;
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
const VIRTUALIZATION_THRESHOLD = 20;
const INITIAL_LOADING_COLUMN_COUNT = 4;
const INITIAL_LOADING_ROW_COUNT = 8;
const LOADING_AGENT_COLUMNS: AgentSummary[] = Array.from(
  { length: 4 },
  (_, index) => ({
    key: `__loading_agent_${index}`,
    label: `loading-${index}`,
    agent: "Loading",
    model: null,
    queueKey: null,
    isModelScoped: false,
  }),
);
const STATUS_FILTER_ORDER: MatrixStatus[] = [
  "queued",
  "running",
  "pass",
  "partial",
  "fail",
  "harness-error",
];

// Row-level filter modes. Inspired by sauron's "any/all pass@k=0" toggle:
// hide tasks where all / any selected agents failed to pass on any trial.
type RowFilterMode = "none" | "allFail" | "anyFail";

const ROW_FILTER_MODES: Array<{
  value: RowFilterMode;
  label: string;
  description: string;
}> = [
  { value: "none", label: "All", description: "Show every task" },
  {
    value: "anyFail",
    label: "Any failed",
    description:
      "Show tasks where at least one agent scored 0 on every trial (partial credit doesn't count as failed)",
  },
  {
    value: "allFail",
    label: "All failed",
    description:
      "Show tasks where every agent scored 0 on every trial (partial credit doesn't count as failed)",
  },
];

const ROW_FILTER_VALUES = new Set<RowFilterMode>([
  "none",
  "allFail",
  "anyFail",
]);

// Baseline agents (nop / oracle) are excluded from row-filter evaluation so
// their deterministic behaviour doesn't influence real-agent analyses.
function isBaselineAgentName(name: string): boolean {
  const lower = name.toLowerCase();
  return (
    lower === "nop" ||
    lower === "oracle" ||
    lower.startsWith("nop-") ||
    lower.startsWith("oracle-") ||
    lower.startsWith("agent-nop") ||
    lower.startsWith("agent-oracle")
  );
}

/**
 * Row-filter evaluation for a single (task, agent) cell.
 *
 * - `"failed"` — agent has ≥1 terminal trial AND every terminal trial scored
 *   exactly 0 reward. Partial credit (0 < reward < 1) is NOT considered failed.
 * - `"scored"` — agent has ≥1 terminal trial with any non-zero reward
 *   (full pass or partial credit).
 * - `null` — agent has no terminal trials yet; skip this cell so still-
 *   running tasks aren't hidden prematurely.
 */
function agentRowFilterStatus(
  trials: readonly Trial[] | undefined,
): "failed" | "scored" | null {
  if (!trials || trials.length === 0) return null;
  let hasTerminal = false;
  for (const trial of trials) {
    if (trial.status !== "success" && trial.status !== "failed") continue;
    hasTerminal = true;
    // Any positive reward — full or partial — disqualifies the agent
    // from counting as "failed" on this task.
    if ((trial.reward ?? 0) > 0) return "scored";
  }
  return hasTerminal ? "failed" : null;
}

/**
 * Reference-style inline action button: transparent by default, subtle
 * hover, disabled in ink-4. Used across the toolbar's "selected"
 * action row (Clear / Rerun / Cancel / Run analysis / Run verdict / Delete).
 */
function InlineBtn({
  onClick,
  disabled,
  children,
  style,
}: {
  onClick?: () => void;
  disabled?: boolean;
  children: React.ReactNode;
  style?: React.CSSProperties;
}) {
  return (
    <Button
      type="button"
      variant="ghost"
      onClick={onClick}
      disabled={disabled}
      style={style}
      className="h-auto gap-1.5 rounded-[5px] bg-transparent px-2 py-1 text-[11.5px] font-medium text-paper-ink-2 transition hover:bg-paper-surface-2 hover:text-paper-ink disabled:cursor-not-allowed disabled:text-paper-ink-4 disabled:hover:bg-transparent disabled:hover:text-paper-ink-4"
    >
      {children}
    </Button>
  );
}

function InlineCount({ children }: { children: React.ReactNode }) {
  return (
    <span className="rounded-[3px] bg-paper-bg-2 px-1.5 py-[1px] font-mono text-[10px] text-paper-ink-2">
      {children}
    </span>
  );
}

// Analysis classification badge styling
const ANALYSIS_CONFIG: Record<
  AnalysisClassification,
  { label: string; dotClass: string }
> = {
  GOOD_SUCCESS: { label: "Good success", dotClass: "bg-emerald-400" },
  GOOD_FAILURE: { label: "Good failure", dotClass: "bg-emerald-400" },
  BAD_SUCCESS: { label: "Bad success", dotClass: "bg-red-400" },
  BAD_FAILURE: { label: "Bad failure", dotClass: "bg-red-400" },
  HARNESS_ERROR: { label: "Harness error", dotClass: "bg-yellow-400" },
};

const ANALYSIS_LEGEND_ITEMS: Array<{
  key: AnalysisLegendKey;
  label: string;
  dotClass: string;
  animate?: boolean;
}> = [
  {
    key: "analyzing",
    label: "Analyzing",
    dotClass: "bg-blue-400",
    animate: true,
  },
  {
    key: "good",
    label: "Pass",
    dotClass: ANALYSIS_CONFIG.GOOD_SUCCESS.dotClass,
  },
  {
    key: "bad",
    label: "Fail",
    dotClass: ANALYSIS_CONFIG.BAD_SUCCESS.dotClass,
  },
  {
    key: "analysis-failed",
    label: "Analysis failed",
    dotClass: "bg-yellow-400",
  },
];

type AnalysisLegendKey = "analyzing" | "good" | "bad" | "analysis-failed";

function getAnalysisLegendKey(trial: Trial): AnalysisLegendKey | null {
  const status = trial.analysis_status;
  const classification = trial.analysis?.classification;

  if (status === "pending" || status === "queued" || status === "running") {
    return "analyzing";
  }

  if (status === "failed") {
    return "analysis-failed";
  }

  if (status === "success") {
    if (
      classification === "GOOD_SUCCESS" ||
      classification === "GOOD_FAILURE"
    ) {
      return "good";
    }
    if (classification === "BAD_SUCCESS" || classification === "BAD_FAILURE") {
      return "bad";
    }
    if (classification === "HARNESS_ERROR") {
      return "analysis-failed";
    }
  }

  return null;
}

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
      dotClass: "bg-yellow-400",
      animate: false,
      title: "Analysis failed",
    };
  }

  return null;
}

function groupTrialsByAgent(
  trials: Trial[] | null | undefined,
  modelScopedAgents: ReadonlySet<string>,
) {
  const grouped = new Map<string, Trial[]>();
  if (!trials) return grouped;
  for (const trial of trials) {
    const key = getExperimentAgentKey(trial, modelScopedAgents);
    const existing = grouped.get(key) ?? [];
    existing.push(trial);
    grouped.set(key, existing);
  }
  return grouped;
}

function hasLiveQueueSnapshot(trial: Trial): boolean {
  return ["queued", "retrying", "running", "pending"].includes(trial.status);
}

function getTrialTitle(trial: Trial, status: MatrixStatus) {
  const reward =
    trial.reward === null
      ? "reward pending"
      : `reward ${formatRewardValue(trial.reward)} (${formatRewardPercent(trial.reward)})`;
  const error = trial.error_message ? ` • ${trial.error_message}` : "";
  const queueInfo = hasLiveQueueSnapshot(trial) ? trial.queue_info : null;
  const queueSnapshot = queueInfo
    ? [
        queueInfo.position != null
          ? `queue #${queueInfo.position}/${queueInfo.queued_count}`
          : null,
        queueInfo.ahead != null ? `${queueInfo.ahead} ahead` : null,
        `${queueInfo.running_count} running`,
        `${queueInfo.concurrency_limit} slots`,
      ]
        .filter((value): value is string => Boolean(value))
        .join(" • ")
    : null;
  const queue = queueSnapshot ? ` • ${queueSnapshot}` : "";
  return `${STATUS_CONFIG[status].shortLabel} • ${trial.status} • ${reward}${error}${queue}`;
}

export function ExperimentTrialsTable({
  tasks,
  agentSummaries,
  modelScopedAgents,
  isLoading,
  isLoadingTrials = false,
  showPassAtK = false,
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
  const DEFAULT_TASK_WIDTH = 240;
  const [taskSearch, setTaskSearch] = useState("");
  const deferredTaskSearch = useDeferredValue(taskSearch);
  const [taskSort, setTaskSort] = useState<
    "default" | "name-asc" | "name-desc"
  >("default");
  const [hiddenAgents, setHiddenAgents] = useState<Set<string>>(new Set());
  const [hoverAgent, setHoverAgent] = useState<string | null>(null);
  const [dimmedStatuses, setDimmedStatuses] = useState<Set<MatrixStatus>>(
    new Set(),
  );
  const [dimmedAnalysisKeys, setDimmedAnalysisKeys] = useState<
    Set<AnalysisLegendKey>
  >(new Set());
  const [rowFilterMode, setRowFilterMode] = useState<RowFilterMode>("none");
  const [selectedTasks, setSelectedTasks] = useState<Set<string>>(new Set());
  const [copiedAgentNameKey, setCopiedAgentNameKey] = useState<string | null>(
    null,
  );
  const [copiedAgentModelKey, setCopiedAgentModelKey] = useState<string | null>(
    null,
  );
  const [copiedTable, setCopiedTable] = useState(false);
  const [deleteTargets, setDeleteTargets] = useState<Task[]>([]);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);
  const [isRerunning, setIsRerunning] = useState(false);
  const [rerunError, setRerunError] = useState<string | null>(null);
  const [isCancellingSelected, setIsCancellingSelected] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [isRunningAnalysis, setIsRunningAnalysis] = useState(false);
  const [analysisError, setAnalysisError] = useState<string | null>(null);
  const [isRunningVerdict, setIsRunningVerdict] = useState(false);
  const [verdictError, setVerdictError] = useState<string | null>(null);
  const [taskColumnWidth, setTaskColumnWidth] = useState(DEFAULT_TASK_WIDTH);
  const [agentColumnWidths, setAgentColumnWidths] = useState<
    Record<string, number>
  >({});
  const tableContainerRef = useRef<HTMLDivElement | null>(null);
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
    analysis: "",
    rowFilter: "",
    taskSearch: "",
  });
  const isFirstFilterSync = useRef(true);

  useEffect(() => {
    const urlHide = searchParams.get("hide") || "";
    const urlDim = searchParams.get("dim") || "";
    const urlAnalysis = searchParams.get("analysis") || "";
    const urlRowFilter = searchParams.get("rowFilter") || "";
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
              value === "queued" ||
              value === "running",
          ),
      );
      setDimmedStatuses(next);
      prevUrlRef.current.dim = urlDim;
    }

    if (urlAnalysis !== prevUrlRef.current.analysis) {
      const next = new Set(
        urlAnalysis
          .split(",")
          .filter(Boolean)
          .filter(
            (value): value is AnalysisLegendKey =>
              value === "analyzing" ||
              value === "good" ||
              value === "bad" ||
              value === "analysis-failed",
          ),
      );
      setDimmedAnalysisKeys(next);
      prevUrlRef.current.analysis = urlAnalysis;
    }

    if (urlRowFilter !== prevUrlRef.current.rowFilter) {
      const next =
        urlRowFilter && ROW_FILTER_VALUES.has(urlRowFilter as RowFilterMode)
          ? (urlRowFilter as RowFilterMode)
          : "none";
      setRowFilterMode(next);
      prevUrlRef.current.rowFilter = urlRowFilter;
    }

    if (urlTaskSearch !== prevUrlRef.current.taskSearch) {
      setTaskSearch(urlTaskSearch);
      prevUrlRef.current.taskSearch = urlTaskSearch;
    }
  }, [searchParams]);

  useEffect(() => {
    if (selectedTasks.size === 0) {
      setRerunError(null);
      setAnalysisError(null);
      setVerdictError(null);
    }
  }, [selectedTasks]);

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
      const analysis = Array.from(dimmedAnalysisKeys).sort();

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

      if (analysis.length > 0) {
        params.set("analysis", analysis.join(","));
      } else {
        params.delete("analysis");
      }

      if (rowFilterMode !== "none") {
        params.set("rowFilter", rowFilterMode);
      } else {
        params.delete("rowFilter");
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
  }, [
    hiddenAgents,
    dimmedStatuses,
    dimmedAnalysisKeys,
    rowFilterMode,
    deferredTaskSearch,
    searchParams,
  ]);

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
      if (a.agent !== b.agent) {
        return a.agent.localeCompare(b.agent);
      }
      return a.label.localeCompare(b.label);
    });
  }, [agentSummaries]);

  const visibleAgents = useMemo(
    () => sortedAgentSummaries.filter((agent) => !hiddenAgents.has(agent.key)),
    [sortedAgentSummaries, hiddenAgents],
  );
  const showLoadingMatrixColumns =
    isLoadingTrials && visibleAgents.length === 0;
  const renderedAgents = showLoadingMatrixColumns
    ? LOADING_AGENT_COLUMNS
    : visibleAgents;

  const columnOrder = useMemo(
    () => ["task", ...renderedAgents.map((agent) => agent.key)],
    [renderedAgents],
  );

  const baseTableWidth = useMemo(() => {
    const agentTotal = renderedAgents.reduce(
      (sum, agent) =>
        sum + (agentColumnWidths[agent.key] ?? DEFAULT_AGENT_WIDTH),
      0,
    );
    return taskColumnWidth + agentTotal;
  }, [renderedAgents, agentColumnWidths, taskColumnWidth, DEFAULT_AGENT_WIDTH]);
  const getDisplayedWidth = (key: "task" | string) => {
    return key === "task"
      ? taskColumnWidth
      : (agentColumnWidths[key] ?? DEFAULT_AGENT_WIDTH);
  };
  const tableMinWidth = Math.max(
    960,
    baseTableWidth,
    columnOrder.length * AGENT_COLUMN_MIN,
  );

  useEffect(() => {
    setAgentColumnWidths((prev) => {
      const next: Record<string, number> = { ...prev };
      let hasChange = false;
      for (const agent of renderedAgents) {
        if (next[agent.key] == null) {
          next[agent.key] = DEFAULT_AGENT_WIDTH;
          hasChange = true;
        }
      }
      return hasChange ? next : prev;
    });
  }, [renderedAgents]);

  // Keys of visible non-baseline agents — the set that row-level filters
  // evaluate against. Hiding an agent (via hide=) or filtering it out
  // because it's a nop/oracle baseline removes it from this set.
  const rowFilterAgentKeys = useMemo(() => {
    return visibleAgents
      .filter((agent) => !isBaselineAgentName(agent.agent))
      .map((agent) => agent.key);
  }, [visibleAgents]);

  const filteredTasks = useMemo(() => {
    const query = deferredTaskSearch.trim().toLowerCase();
    const searchFiltered = query
      ? tasks.filter((task) => {
          // Comma-separated queries use OR logic (any substring matches).
          const terms = query
            .split(",")
            .map((t) => t.trim())
            .filter(Boolean);
          if (terms.length === 0) return true;
          const haystack = [task.name, task.task_path, task.id]
            .filter(Boolean)
            .join(" ")
            .toLowerCase();
          return terms.some((term) => haystack.includes(term));
        })
      : tasks;

    // Apply row-level filter using visible non-baseline agents.
    const rowFiltered =
      rowFilterMode === "none" || rowFilterAgentKeys.length === 0
        ? searchFiltered
        : searchFiltered.filter((task) => {
            const trialsByAgent = groupTrialsByAgent(
              task.trials,
              modelScopedAgents,
            );
            // Derive failed/scored per evaluated agent; skip agents that
            // have no terminal trials yet so running tasks aren't hidden
            // early. Partial credit (0 < reward < 1) counts as "scored".
            const perAgent = rowFilterAgentKeys
              .map((key) => agentRowFilterStatus(trialsByAgent.get(key)))
              .filter((r): r is "failed" | "scored" => r !== null);
            if (perAgent.length === 0) return true;
            const failCount = perAgent.filter((r) => r === "failed").length;
            if (rowFilterMode === "allFail") {
              return failCount === perAgent.length;
            }
            if (rowFilterMode === "anyFail") {
              return failCount > 0;
            }
            return true;
          });

    if (taskSort === "default") return rowFiltered;
    const nameOf = (task: Task) => task.name ?? task.task_path ?? task.id;
    const sorted = [...rowFiltered].sort((a, b) =>
      nameOf(a).localeCompare(nameOf(b), undefined, {
        numeric: true,
        sensitivity: "base",
      }),
    );
    return taskSort === "name-desc" ? sorted.reverse() : sorted;
  }, [
    tasks,
    deferredTaskSearch,
    taskSort,
    rowFilterMode,
    rowFilterAgentKeys,
    modelScopedAgents,
  ]);

  const getTaskContext = useMemo(() => {
    const contextCache = new WeakMap<
      Task,
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
      const cached = contextCache.get(task);
      if (cached) return cached;

      const groupedTrialsByAgent = groupTrialsByAgent(
        task.trials,
        modelScopedAgents,
      );
      const orderedTrials: Trial[] = [];
      const trialIndexById = new Map<string, number>();
      const trialGroups: Array<{
        agent: string;
        model: string | null;
        trials: Trial[];
      }> = [];

      for (const agent of visibleAgents) {
        const trials = groupedTrialsByAgent.get(agent.key) ?? EMPTY_TRIALS;
        if (trials.length > 0) {
          trialGroups.push({
            agent: agent.label,
            model: agent.model,
            trials,
          });
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
      contextCache.set(task, context);
      return context;
    };
  }, [visibleAgents, modelScopedAgents]);

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

  const selectedCancellableTasks = useMemo(
    () => selectedTaskList.filter((task) => taskHasCancellableWork(task)),
    [selectedTaskList],
  );

  const selectedAnalysisRunnableTasks = useMemo(
    () =>
      selectedTaskList.filter((task) => {
        const trials = task.trials ?? [];
        if (trials.length === 0) return false;
        const allTrialsTerminal = trials.every(
          (trial) => trial.status === "failed" || trial.status === "success",
        );
        const hasAnalysisInFlight = trials.some((trial) =>
          isActivePipelineStatus(trial.analysis_status),
        );
        const verdictInFlight = isActivePipelineStatus(task.verdict_status);
        return allTrialsTerminal && !hasAnalysisInFlight && !verdictInFlight;
      }),
    [selectedTaskList],
  );

  const selectedVerdictRunnableTasks = useMemo(
    () =>
      selectedTaskList.filter((task) => {
        const trials = task.trials ?? [];
        if (trials.length === 0) return false;
        const allTrialsTerminal = trials.every(
          (trial) => trial.status === "failed" || trial.status === "success",
        );
        const allAnalysesComplete = trials.every(
          (trial) =>
            trial.analysis_status === "success" ||
            trial.analysis_status === "failed",
        );
        const verdictInFlight = isActivePipelineStatus(task.verdict_status);
        return allTrialsTerminal && allAnalysesComplete && !verdictInFlight;
      }),
    [selectedTaskList],
  );

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

  const toggleAnalysisKey = (analysisKey: AnalysisLegendKey) => {
    setDimmedAnalysisKeys((prev) => {
      const next = new Set(prev);
      if (next.has(analysisKey)) {
        next.delete(analysisKey);
      } else {
        next.add(analysisKey);
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

  const handleCopyAgentName = async (agentKey: string, agentName: string) => {
    await navigator.clipboard.writeText(agentName);
    setCopiedAgentNameKey(agentKey);
    window.setTimeout(() => {
      setCopiedAgentNameKey((prev) => (prev === agentKey ? null : prev));
    }, 2000);
  };

  const handleCopyAgentModel = async (agentKey: string, modelId: string) => {
    await navigator.clipboard.writeText(modelId);
    setCopiedAgentModelKey(agentKey);
    window.setTimeout(() => {
      setCopiedAgentModelKey((prev) => (prev === agentKey ? null : prev));
    }, 2000);
  };

  const handleCopyTableAsTSV = async () => {
    // Generate TSV header
    const headers = ["Task", ...visibleAgents.map((agent) => agent.label)];
    const rows: string[] = [headers.join("\t")];

    // Generate TSV rows
    for (const task of filteredTasks) {
      const grouped =
        getTaskContext(task).groupedTrialsByAgent ?? EMPTY_TRIAL_MAP;

      const rowCells = [task.name];
      for (const agent of visibleAgents) {
        const trials = grouped.get(agent.key) ?? [];
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
      onRerun?.(selectedTaskList.map((task) => task.id));
    } finally {
      setIsRerunning(false);
    }
  };

  const handleCancelSelectedTasks = async () => {
    if (isCancellingSelected || selectedCancellableTasks.length === 0) return;

    setIsCancellingSelected(true);
    setCancelError(null);

    try {
      const taskIds = selectedCancellableTasks.map((task) => task.id);
      const res = await fetch(`/api/tasks/cancel`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task_ids: taskIds }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || data.error || "Failed to cancel tasks");
      }

      setCancelError(null);
      onRerun?.(selectedCancellableTasks.map((task) => task.id));
    } catch (error) {
      setCancelError(
        error instanceof Error ? error.message : "Failed to cancel tasks",
      );
    } finally {
      setIsCancellingSelected(false);
    }
  };

  const handleRunAnalysisForSelectedTasks = async () => {
    if (!canRerun || isRunningAnalysis) return;
    if (selectedAnalysisRunnableTasks.length === 0) {
      setAnalysisError("No tasks are ready for analysis.");
      return;
    }

    setIsRunningAnalysis(true);
    setAnalysisError(null);

    try {
      const results = await Promise.allSettled(
        selectedAnalysisRunnableTasks.map(async (task) => {
          const res = await fetch(`/api/tasks/${task.id}/analysis/retry`, {
            method: "POST",
          });
          if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            throw new Error(
              data.detail || data.error || "Failed to queue task analysis",
            );
          }
        }),
      );

      const failures = results.filter((result) => result.status === "rejected");
      if (failures.length > 0) {
        setAnalysisError(
          `Failed to queue analysis for ${failures.length} task(s).`,
        );
      } else {
        setAnalysisError(null);
      }
      onRerun?.(selectedAnalysisRunnableTasks.map((task) => task.id));
    } finally {
      setIsRunningAnalysis(false);
    }
  };

  const handleRunVerdictForSelectedTasks = async () => {
    if (!canRerun || isRunningVerdict) return;
    if (selectedVerdictRunnableTasks.length === 0) {
      setVerdictError("No tasks are ready for a verdict.");
      return;
    }

    setIsRunningVerdict(true);
    setVerdictError(null);

    try {
      const results = await Promise.allSettled(
        selectedVerdictRunnableTasks.map(async (task) => {
          const res = await fetch(`/api/tasks/${task.id}/verdict/retry`, {
            method: "POST",
          });
          if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            throw new Error(
              data.detail || data.error || "Failed to queue task verdict",
            );
          }
        }),
      );

      const failures = results.filter((result) => result.status === "rejected");
      if (failures.length > 0) {
        setVerdictError(
          `Failed to queue verdict for ${failures.length} task(s).`,
        );
      } else {
        setVerdictError(null);
      }
      onRerun?.(selectedVerdictRunnableTasks.map((task) => task.id));
    } finally {
      setIsRunningVerdict(false);
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

  const isInitialLoading = isLoading && tasks.length === 0;

  if (isInitialLoading) {
    return (
      <div className="space-y-4">
        {showPassAtK ? (
          <div className="grid items-stretch gap-4 xl:grid-cols-2">
            <div className="rounded-lg border border-border bg-card p-4 shadow-xs">
              <Skeleton className="h-5 w-36" />
              <Skeleton className="mt-4 h-56 w-full" />
            </div>
            <div className="rounded-lg border border-border bg-card p-4 shadow-xs">
              <Skeleton className="h-5 w-40" />
              <Skeleton className="mt-4 h-56 w-full" />
            </div>
          </div>
        ) : null}

        <div className="max-w-full overflow-hidden rounded-lg border border-border bg-card shadow-xs">
          <div className="relative z-30 space-y-3 border-b border-border bg-card/70 px-3 py-3">
            <div className="flex flex-wrap items-start gap-3">
              <Skeleton className="h-9 w-full sm:w-[320px]" />
              <div className="min-w-0 flex-1">
                <div className="grid w-full min-w-0 grid-cols-[56px_minmax(0,1fr)] gap-x-3 gap-y-2 sm:ml-auto sm:w-fit">
                  <Skeleton className="h-4 w-10 self-center" />
                  <div className="flex min-w-0 flex-wrap items-center gap-2 sm:justify-end">
                    {Array.from({ length: 6 }).map((_, index) => (
                      <Skeleton key={index} className="h-6 w-24" />
                    ))}
                  </div>
                  <Skeleton className="h-4 w-14 self-center" />
                  <div className="flex min-w-0 flex-wrap items-center gap-2 sm:justify-end">
                    {Array.from({ length: 4 }).map((_, index) => (
                      <Skeleton key={index} className="h-6 w-28" />
                    ))}
                  </div>
                </div>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              Loading experiment tasks and trial matrix...
              <div className="ml-auto flex flex-wrap items-center gap-2">
                <Skeleton className="h-7 w-32" />
                <Skeleton className="h-7 w-24" />
                <Skeleton className="h-7 w-28" />
                <Skeleton className="h-7 w-24" />
              </div>
            </div>
          </div>

          <div className="overflow-x-auto p-3">
            <div className="w-full min-w-[960px] space-y-2">
              <div
                className="grid gap-2 rounded-md bg-muted/40 p-2"
                style={{
                  gridTemplateColumns: `240px repeat(${INITIAL_LOADING_COLUMN_COUNT}, minmax(0, 1fr))`,
                }}
              >
                <Skeleton className="h-5 w-24" />
                {Array.from({ length: INITIAL_LOADING_COLUMN_COUNT }).map(
                  (_, index) => (
                    <Skeleton key={index} className="h-5 w-full" />
                  ),
                )}
              </div>

              {Array.from({ length: INITIAL_LOADING_ROW_COUNT }).map(
                (_, rowIndex) => (
                  <div
                    key={rowIndex}
                    className="grid gap-2 rounded-md border border-border/60 p-2"
                    style={{
                      gridTemplateColumns: `240px repeat(${INITIAL_LOADING_COLUMN_COUNT}, minmax(0, 1fr))`,
                    }}
                  >
                    <div className="flex items-center gap-2">
                      <Skeleton className="h-4 w-4 rounded-sm" />
                      <Skeleton className="h-4 w-40" />
                    </div>
                    {Array.from({ length: INITIAL_LOADING_COLUMN_COUNT }).map(
                      (_, columnIndex) => (
                        <div
                          key={columnIndex}
                          className="flex items-center justify-center gap-1"
                        >
                          <Skeleton className="h-5 w-5 rounded-sm" />
                          <Skeleton className="h-5 w-5 rounded-sm" />
                        </div>
                      ),
                    )}
                  </div>
                ),
              )}
            </div>
          </div>
        </div>
      </div>
    );
  }

  // Partial outcomes are rendered as numeric colored tiles (not a single color
  // chip), so we don't expose them in the trial-outcome legend filter.
  const LEGEND_STATUS_ORDER = STATUS_FILTER_ORDER.filter(
    (s) => s !== "partial",
  );

  const renderStatusChip = (status: MatrixStatus) => {
    const config = STATUS_CONFIG[status];
    const isDimmed = dimmedStatuses.has(status);
    return (
      <Tooltip key={status}>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            onClick={() => toggleStatus(status)}
            className={`h-auto select-none gap-1.5 rounded-[5px] border border-transparent px-2 py-1 text-[11px] font-medium text-[color:var(--paper-ink-2)] transition hover:bg-[color:var(--paper-surface-2)] hover:text-[color:var(--paper-ink)] ${
              isDimmed ? "line-through opacity-[0.38]" : ""
            }`}
          >
            <span
              className={`inline-flex items-center justify-center border-transparent ${STATUS_GLYPH_BOX} ${config.matrixClass}`}
            >
              <StatusIcon status={status} />
            </span>
            <span>{config.shortLabel}</span>
          </Button>
        </TooltipTrigger>
        <TooltipContent>
          {config.shortLabel} ({isDimmed ? "dimmed" : "visible"})
        </TooltipContent>
      </Tooltip>
    );
  };

  // Paper-palette analyzer dot color for the legend chip, keyed by
  // AnalysisLegendKey.
  const ANALYZER_CHIP_COLOR: Record<AnalysisLegendKey, string> = {
    analyzing: "var(--paper-a-analyzing)",
    good: "var(--paper-a-good)",
    bad: "var(--paper-a-bad)",
    "analysis-failed": "var(--paper-a-failed)",
  };

  const renderAnalyzerChip = (item: (typeof ANALYSIS_LEGEND_ITEMS)[number]) => {
    const isDimmed = dimmedAnalysisKeys.has(item.key);
    return (
      <Tooltip key={item.key}>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            onClick={() => toggleAnalysisKey(item.key)}
            className={`h-auto select-none gap-1.5 rounded-[5px] border border-transparent px-2 py-1 text-[11px] font-medium text-[color:var(--paper-ink-2)] transition hover:bg-[color:var(--paper-surface-2)] hover:text-[color:var(--paper-ink)] ${
              isDimmed ? "line-through opacity-[0.38]" : ""
            }`}
          >
            <span
              className={`inline-block h-2 w-2 rounded-full ${item.animate ? "animate-pulse" : ""}`}
              style={{ background: ANALYZER_CHIP_COLOR[item.key] }}
            />
            <span>{item.label}</span>
          </Button>
        </TooltipTrigger>
        <TooltipContent>
          {item.label} ({isDimmed ? "dimmed" : "visible"})
        </TooltipContent>
      </Tooltip>
    );
  };

  const renderLegendAnatomy = () => (
    <Tooltip>
      <TooltipTrigger asChild>
        <div className="flex items-center gap-2.5 border-r border-dashed border-[color:var(--paper-line)] pl-1.5 pr-2.5 font-mono text-[9.5px] leading-tight text-[color:var(--paper-ink-3)]">
          <span className="relative inline-flex">
            <span
              className={`flex items-center justify-center border-transparent bg-[color:var(--paper-pass)] text-white ${STATUS_GLYPH_BOX}`}
            >
              <StatusIcon status="pass" />
            </span>
            <span className="absolute -right-[2px] -top-[2px] h-[7px] w-[7px] rounded-full bg-[color:var(--paper-a-good)] ring-[1.5px] ring-[color:var(--paper-surface)]" />
          </span>
          <span className="flex flex-col gap-0.5">
            <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
              <span className="inline-block h-2.5 w-2.5 rounded-[2px] bg-[color:var(--paper-pass)]" />
              trial outcome
            </span>
            <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
              <span className="mx-[1px] inline-block h-2 w-2 rounded-full bg-[color:var(--paper-a-good)]" />
              trial analysis
            </span>
          </span>
        </div>
      </TooltipTrigger>
      <TooltipContent>How to read a cell</TooltipContent>
    </Tooltip>
  );

  const renderAgentFilterMenu = () => (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          className="h-auto select-none gap-1.5 rounded-[5px] border border-[color:var(--paper-line)] bg-transparent px-2 py-1 text-[11.5px] font-medium text-[color:var(--paper-ink-2)] transition hover:bg-[color:var(--paper-surface-2)] hover:text-[color:var(--paper-ink)]"
        >
          Agents
          <InlineCount>
            {visibleAgents.length}/{sortedAgentSummaries.length}
          </InlineCount>
          <ChevronDown className="h-3 w-3" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="max-h-64 w-64 overflow-auto p-2">
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
            const isVisible = !hiddenAgents.has(agent.key);
            return (
              <Label
                key={agent.key}
                className={`flex items-center gap-2 rounded px-2 py-1 text-xs font-normal ${
                  isVisible ? "hover:bg-muted" : "text-muted-foreground"
                }`}
              >
                <Checkbox
                  checked={isVisible}
                  onCheckedChange={() => toggleAgent(agent.key)}
                  className="h-3.5 w-3.5"
                />
                <span className={`${isVisible ? "" : "line-through"}`}>
                  {agent.label}
                </span>
                <span className="flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
                  <QueueKeyIcon
                    queueKey={agent.queueKey}
                    model={agent.model}
                    size={10}
                    className="shrink-0"
                  />
                  {agent.model ?? "—"}
                </span>
              </Label>
            );
          })}
        </div>
      </PopoverContent>
    </Popover>
  );

  const renderRowFilterControl = () => {
    const hasAgentsToFilter = rowFilterAgentKeys.length > 0;
    return (
      <div
        role="group"
        aria-label="Row filter"
        className="inline-flex items-center rounded-md border border-border bg-background p-0.5"
      >
        {ROW_FILTER_MODES.map((mode) => {
          const active = rowFilterMode === mode.value;
          const disabled = !hasAgentsToFilter && mode.value !== "none";
          return (
            <Tooltip key={mode.value}>
              <TooltipTrigger asChild>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={disabled}
                  onClick={() => setRowFilterMode(mode.value)}
                  className={`h-auto select-none rounded-sm px-2 py-1 text-[10px] font-semibold uppercase tracking-wide transition-colors ${
                    active
                      ? "bg-muted text-foreground"
                      : "text-muted-foreground hover:text-foreground"
                  }`}
                  aria-pressed={active}
                >
                  {mode.label}
                </Button>
              </TooltipTrigger>
              <TooltipContent>{mode.description}</TooltipContent>
            </Tooltip>
          );
        })}
      </div>
    );
  };

  const renderLegendBlock = () => (
    <div className="flex min-w-0 flex-wrap items-center gap-y-1 rounded-[8px] border border-[color:var(--paper-line)] bg-[color:var(--paper-bg)] p-1 sm:ml-auto sm:w-fit sm:flex-nowrap">
      {renderLegendAnatomy()}
      <div className="flex items-center gap-0.5 px-1">
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="cursor-help whitespace-nowrap pr-2 font-mono text-[9.5px] font-semibold uppercase tracking-[0.1em] text-[color:var(--paper-ink-3)]">
              Trial outcome
            </span>
          </TooltipTrigger>
          <TooltipContent className="max-w-xs">
            Did the agent&apos;s trial run succeed? Produced by the harness when
            the agent finishes or errors.
          </TooltipContent>
        </Tooltip>
        {LEGEND_STATUS_ORDER.map((status) => renderStatusChip(status))}
      </div>
      <div className="flex items-center gap-0.5 border-l border-dashed border-[color:var(--paper-line)] pl-2 ml-1">
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="cursor-help whitespace-nowrap pr-2 font-mono text-[9.5px] font-semibold uppercase tracking-[0.1em] text-[color:var(--paper-ink-3)]">
              Trial analysis
            </span>
          </TooltipTrigger>
          <TooltipContent className="max-w-xs">
            A second pass — an LLM grades the trial output. Only present for
            trials that were sent for analysis.
          </TooltipContent>
        </Tooltip>
        {ANALYSIS_LEGEND_ITEMS.map((item) => renderAnalyzerChip(item))}
      </div>
    </div>
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
        {/* Pass@k Graph - only shows when there are multiple trials per task-agent */}
        {showPassAtK ? (
          <div className="grid items-stretch gap-4 xl:grid-cols-2">
            <div className="h-full min-w-0">
              <PassAtKGraph
                tasks={tasks}
                agentSummaries={sortedAgentSummaries}
                hiddenAgents={hiddenAgents}
                onToggleAgent={toggleAgent}
                hoverAgent={hoverAgent}
                onHoverAgent={setHoverAgent}
              />
            </div>
            <div className="h-full min-w-0">
              <PassAtOneLeaderboard
                tasks={tasks}
                agentSummaries={sortedAgentSummaries}
                hiddenAgents={hiddenAgents}
                onToggleAgent={toggleAgent}
                hoverAgent={hoverAgent}
                onHoverAgent={setHoverAgent}
              />
            </div>
          </div>
        ) : null}

        <div className="max-w-full overflow-hidden rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)]">
          <div className="relative z-30 flex flex-col gap-3 border-b border-[color:var(--paper-line-2)] bg-[color:var(--paper-surface)] px-4 pb-3 pt-3.5">
            <div className="flex flex-wrap items-start gap-3">
              <div className="w-full sm:w-[280px]">
                <div className="flex h-8 items-center gap-2 rounded-[7px] border border-[color:var(--paper-line)] bg-[color:var(--paper-bg)] px-2.5 text-[color:var(--paper-ink-2)] focus-within:border-[color:var(--paper-ink-4)]">
                  <Search className="h-3.5 w-3.5 shrink-0 text-[color:var(--paper-ink-3)]" />
                  <Input
                    type="search"
                    value={taskSearch}
                    onChange={(event) =>
                      handleTaskSearchChange(event.target.value)
                    }
                    placeholder="Search tasks (comma-separated)"
                    className="h-auto min-w-0 flex-1 rounded-none border-0 bg-transparent p-0 text-[12.5px] text-[color:var(--paper-ink)] placeholder:text-[color:var(--paper-ink-3)] focus-visible:ring-0 focus-visible:ring-offset-0"
                  />
                </div>
              </div>
              <div className="min-w-0 flex-1">{renderLegendBlock()}</div>
            </div>
            <div className="flex flex-wrap items-center gap-1.5 text-[11.5px] text-[color:var(--paper-ink-3)]">
              {!readOnly && (
                <>
                  <span>{selectedTasks.size} selected</span>
                  <InlineBtn
                    onClick={clearSelection}
                    disabled={selectedTasks.size === 0}
                  >
                    Clear
                  </InlineBtn>
                  <span className="select-none text-[color:var(--paper-line)]">
                    │
                  </span>
                  {canRerun && (
                    <InlineBtn
                      onClick={handleRerunSelectedTasks}
                      disabled={
                        isRerunning || selectedRetryableTrials.length === 0
                      }
                    >
                      {isRerunning ? "Rerunning" : "Rerun trials"}
                      <InlineCount>
                        {selectedRetryableTrials.length}
                      </InlineCount>
                    </InlineBtn>
                  )}
                  {canRerun && (
                    <InlineBtn
                      onClick={handleCancelSelectedTasks}
                      disabled={
                        isCancellingSelected ||
                        selectedCancellableTasks.length === 0
                      }
                    >
                      {isCancellingSelected ? (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      ) : (
                        <OctagonX className="h-3 w-3" />
                      )}
                      {isCancellingSelected ? "Cancelling" : "Cancel"}
                      <InlineCount>
                        {selectedCancellableTasks.length}
                      </InlineCount>
                    </InlineBtn>
                  )}
                  <span className="select-none text-[color:var(--paper-line)]">
                    │
                  </span>
                  {canRerun && (
                    <InlineBtn
                      onClick={handleRunAnalysisForSelectedTasks}
                      disabled={
                        isRunningAnalysis ||
                        selectedAnalysisRunnableTasks.length === 0
                      }
                    >
                      {isRunningAnalysis ? "Queueing" : "Run analysis"}
                      <InlineCount>
                        {selectedAnalysisRunnableTasks.length}
                      </InlineCount>
                    </InlineBtn>
                  )}
                  {canRerun && (
                    <InlineBtn
                      onClick={handleRunVerdictForSelectedTasks}
                      disabled={
                        isRunningVerdict ||
                        selectedVerdictRunnableTasks.length === 0
                      }
                    >
                      {isRunningVerdict ? "Queueing" : "Run verdict"}
                      <InlineCount>
                        {selectedVerdictRunnableTasks.length}
                      </InlineCount>
                    </InlineBtn>
                  )}
                  {canDeleteTasks && (
                    <>
                      <span className="select-none text-[color:var(--paper-line)]">
                        │
                      </span>
                      <InlineBtn
                        onClick={() => {
                          setDeleteTargets(selectedTaskList);
                          setDeleteError(null);
                        }}
                        disabled={isDeleting || selectedTaskList.length === 0}
                        style={
                          selectedTaskList.length > 0 && !isDeleting
                            ? { color: "var(--paper-fail)" }
                            : undefined
                        }
                      >
                        <Trash2 className="h-3 w-3" />
                        Delete
                      </InlineBtn>
                    </>
                  )}
                </>
              )}
              {cancelError && (
                <span className="text-[10px] text-[color:var(--paper-fail)]">
                  {cancelError}
                </span>
              )}
              {rerunError && (
                <span className="text-[10px] text-[color:var(--paper-fail)]">
                  {rerunError}
                </span>
              )}
              {analysisError && (
                <span className="text-[10px] text-[color:var(--paper-fail)]">
                  {analysisError}
                </span>
              )}
              {verdictError && (
                <span className="text-[10px] text-[color:var(--paper-fail)]">
                  {verdictError}
                </span>
              )}
              <div
                className={`flex flex-wrap items-center gap-1.5 ${readOnly ? "" : "ml-auto"}`}
              >
                {renderRowFilterControl()}
                {renderAgentFilterMenu()}
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      type="button"
                      variant="ghost"
                      onClick={handleCopyTableAsTSV}
                      className="h-auto select-none gap-1.5 rounded-[5px] border border-[color:var(--paper-line)] bg-transparent px-2 py-1 text-[11.5px] font-medium text-[color:var(--paper-ink-2)] transition hover:bg-[color:var(--paper-surface-2)] hover:text-[color:var(--paper-ink)]"
                    >
                      {copiedTable ? (
                        <>
                          <Check className="h-3 w-3 text-[color:var(--paper-pass)]" />
                          Copied
                        </>
                      ) : (
                        <>
                          <Copy className="h-3 w-3" />
                          Copy TSV
                        </>
                      )}
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>Copy table as TSV</TooltipContent>
                </Tooltip>
              </div>
            </div>
          </div>
          <div
            ref={tableContainerRef}
            className={`max-h-[70vh] overflow-x-auto overflow-y-auto ${isResizing ? "select-none" : ""}`}
          >
            <table
              className="w-full min-w-[960px] caption-bottom text-sm"
              style={{
                tableLayout: "fixed",
                width: "100%",
                minWidth: tableMinWidth,
              }}
            >
              <colgroup>
                <col style={{ width: `${getDisplayedWidth("task")}px` }} />
                {renderedAgents.map((agent) => (
                  <col
                    key={`col-${agent.key}`}
                    style={{
                      width: `${getDisplayedWidth(agent.key)}px`,
                    }}
                  />
                ))}
              </colgroup>
              <TableHeader className="sticky top-0 z-20 bg-[color:var(--paper-surface-2)]">
                <TableRow className="border-b border-[color:var(--paper-line)] hover:bg-transparent">
                  <TableHead
                    className="relative sticky left-0 z-30 h-auto border-r border-[color:var(--paper-line)] bg-[color:var(--paper-surface-2)] px-3 py-3 font-mono font-bold text-[color:var(--paper-ink)] [&:has([role=checkbox])]:pr-3"
                    style={{ width: getDisplayedWidth("task") }}
                  >
                    <div className="flex items-center gap-2">
                      <span className="w-5 shrink-0 text-right text-[10px] text-muted-foreground">
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
                      <Button
                        type="button"
                        variant="ghost"
                        onClick={() =>
                          setTaskSort((prev) =>
                            prev === "default"
                              ? "name-asc"
                              : prev === "name-asc"
                                ? "name-desc"
                                : "default",
                          )
                        }
                        title={
                          taskSort === "default"
                            ? "Sort by task name (A→Z)"
                            : taskSort === "name-asc"
                              ? "Sort by task name (Z→A)"
                              : "Clear sort (default order)"
                        }
                        aria-label="Toggle task sort"
                        className="h-auto gap-1 rounded-sm bg-transparent px-1 py-0 text-xs font-normal transition hover:bg-background/70 hover:text-blue-400 sm:text-sm"
                      >
                        <span>Task</span>
                        {taskSort === "name-asc" ? (
                          <ArrowUp className="h-3 w-3" />
                        ) : taskSort === "name-desc" ? (
                          <ArrowDown className="h-3 w-3" />
                        ) : (
                          <ArrowUpDown className="h-3 w-3 text-muted-foreground/60" />
                        )}
                      </Button>
                    </div>
                    <div
                      className="absolute right-0 top-0 h-full w-1.5 cursor-col-resize"
                      onMouseDown={(event) =>
                        startResize(event, "task", taskColumnWidth)
                      }
                    />
                  </TableHead>
                  {renderedAgents.map((agent, agentIndex) => (
                    <TableHead
                      key={agent.key}
                      className="relative h-auto border-r border-[color:var(--paper-line)] bg-[color:var(--paper-surface-2)] px-1 py-3 text-center font-mono last:border-r-0 sm:px-2"
                      style={{
                        width: getDisplayedWidth(agent.key),
                      }}
                    >
                      {showLoadingMatrixColumns ? (
                        <div className="flex min-w-[60px] flex-col items-center gap-2 py-1 sm:min-w-[80px] md:min-w-[100px]">
                          <Skeleton className="h-3 w-16" />
                          <Skeleton className="h-3 w-20" />
                        </div>
                      ) : (
                        <div className="flex min-w-[60px] flex-col items-center gap-0.5 sm:min-w-[80px] md:min-w-[100px]">
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Button
                                type="button"
                                variant="ghost"
                                onClick={() =>
                                  handleCopyAgentName(agent.key, agent.agent)
                                }
                                className="h-auto max-w-[70px] gap-1 rounded-sm bg-transparent px-1 py-0 text-[10px] font-bold text-foreground transition hover:bg-background/70 hover:text-blue-400 sm:max-w-[110px] sm:text-xs md:max-w-none"
                                aria-label={`Copy agent name ${agent.agent}`}
                                title="Copy agent name"
                              >
                                <QueueKeyIcon
                                  agent={agent.agent}
                                  size={12}
                                  className="shrink-0"
                                />
                                <span className="min-w-0 truncate">
                                  {copiedAgentNameKey === agent.key
                                    ? "Copied"
                                    : agent.agent}
                                </span>
                              </Button>
                            </TooltipTrigger>
                            <TooltipContent side="bottom">
                              {copiedAgentNameKey === agent.key
                                ? "Copied agent name"
                                : agent.agent}
                            </TooltipContent>
                          </Tooltip>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              {agent.model ? (
                                <Button
                                  type="button"
                                  variant="ghost"
                                  onClick={() =>
                                    handleCopyAgentModel(
                                      agent.key,
                                      agent.model!,
                                    )
                                  }
                                  className="h-auto w-full min-w-0 gap-1 rounded-sm bg-transparent px-1 py-0 font-mono text-[9px] font-normal text-muted-foreground transition hover:bg-background/70 hover:text-foreground sm:text-[10px]"
                                  aria-label={`Copy model id ${agent.model}`}
                                  title="Copy model id"
                                >
                                  {copiedAgentModelKey === agent.key ? (
                                    <Check className="h-3 w-3 shrink-0 text-emerald-500" />
                                  ) : (
                                    <QueueKeyIcon
                                      queueKey={agent.queueKey}
                                      model={agent.model}
                                      size={10}
                                      className="shrink-0"
                                    />
                                  )}
                                  <span className="min-w-0 truncate">
                                    {agent.model}
                                  </span>
                                </Button>
                              ) : (
                                <div className="flex w-full min-w-0 items-center justify-center gap-1 font-mono text-[9px] font-normal text-muted-foreground sm:text-[10px]">
                                  <span className="min-w-0 truncate">—</span>
                                </div>
                              )}
                            </TooltipTrigger>
                            <TooltipContent side="bottom">
                              {copiedAgentModelKey === agent.key
                                ? "Copied model id"
                                : (agent.model ?? "—")}
                            </TooltipContent>
                          </Tooltip>
                        </div>
                      )}
                      {agentIndex < renderedAgents.length - 1 &&
                        !showLoadingMatrixColumns && (
                          <div
                            className="absolute right-0 top-0 h-full w-1.5 cursor-col-resize"
                            onMouseDown={(event) =>
                              startResize(
                                event,
                                agent.key,
                                agentColumnWidths[agent.key] ??
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
                      colSpan={Math.max(1, renderedAgents.length + 1)}
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
                  const isTrialDataPending =
                    isLoadingTrials && task.trials == null;
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
                      className="group bg-[color:var(--paper-surface)] hover:bg-[color:var(--paper-surface-2)] [&_td]:hover:!bg-[color:var(--paper-surface-2)]"
                    >
                      <TableCell
                        className="sticky left-0 z-10 border-b border-r border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-3.5 py-2.5 font-mono text-xs text-[color:var(--paper-ink)] [&:has([role=checkbox])]:pr-3.5"
                        style={{
                          width: getDisplayedWidth("task"),
                        }}
                      >
                        <div className="flex min-w-0 items-center gap-2">
                          <span className="w-5 shrink-0 text-right text-[10px] text-muted-foreground">
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
                          <div className="flex min-w-0 flex-1 items-center gap-2">
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <Button
                                  type="button"
                                  variant="ghost"
                                  onClick={() =>
                                    onTaskSelect?.(task, {
                                      orderedTasks: filteredTasks,
                                      taskIndex: index,
                                    })
                                  }
                                  className="h-auto min-w-0 flex-1 cursor-pointer justify-start truncate bg-transparent p-0 text-left font-mono text-[11.5px] font-normal text-[color:var(--paper-ink)] transition-colors hover:bg-transparent hover:text-[color:oklch(40%_0.1_240)]"
                                >
                                  {task.name}
                                </Button>
                              </TooltipTrigger>
                              <TooltipContent>View task files</TooltipContent>
                            </Tooltip>
                            {task.current_version != null && (
                              <span className="inline-flex shrink-0 items-center rounded-[3px] bg-[color:var(--paper-bg-2)] px-1 py-px font-mono text-[9.5px] font-medium leading-none text-[color:var(--paper-ink-3)]">
                                v{task.current_version}
                              </span>
                            )}
                          </div>
                        </div>
                      </TableCell>
                      {renderedAgents.map((agent) => {
                        const trials = grouped.get(agent.key) ?? EMPTY_TRIALS;
                        return (
                          <TableCell
                            key={`${task.id}-${agent.key}`}
                            className="border-b border-r border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-3.5 py-2 text-center last:border-r-0"
                            style={{
                              width: getDisplayedWidth(agent.key),
                            }}
                          >
                            {trials.length === 0 ? (
                              isTrialDataPending ? (
                                <div className="flex items-center justify-center gap-1">
                                  <Skeleton className="h-5 w-5 rounded-sm" />
                                  <Skeleton className="h-5 w-5 rounded-sm" />
                                </div>
                              ) : (
                                <span className="text-xs text-muted-foreground">
                                  —
                                </span>
                              )
                            ) : (
                              <div className="flex flex-wrap justify-center gap-[3px]">
                                {trials.map((trial, trialIndex) => {
                                  const status = getMatrixStatus(
                                    trial.status,
                                    trial.reward,
                                    trial.error_message,
                                  );
                                  const config = STATUS_CONFIG[status];
                                  const isDimmed = dimmedStatuses.has(status);
                                  const dimClass =
                                    isDimmed && status !== "harness-error"
                                      ? "opacity-25"
                                      : "";
                                  const analysisIndicator =
                                    getAnalysisIndicator(trial);
                                  const analysisLegendKey =
                                    getAnalysisLegendKey(trial);
                                  const analysisDimClass =
                                    analysisLegendKey &&
                                    dimmedAnalysisKeys.has(analysisLegendKey)
                                      ? "opacity-25"
                                      : "";
                                  const baseTitle = getTrialTitle(
                                    trial,
                                    status,
                                  );
                                  const isPartial = status === "partial";
                                  const partialLabel = isPartial
                                    ? formatPartialRewardBadgeValue(
                                        trial.reward,
                                      )
                                    : null;
                                  const analysisTitle = analysisIndicator
                                    ? ` · ${analysisIndicator.title}`
                                    : "";
                                  const fullTitle = `${baseTitle}${analysisTitle}`;
                                  return (
                                    <span
                                      key={trial.id}
                                      className={`relative inline-flex ${dimClass || analysisDimClass ? "opacity-25" : ""}`}
                                    >
                                      <Button
                                        type="button"
                                        variant="unstyled"
                                        onClick={() => {
                                          const trialIndexInGroup =
                                            trialIndexById.get(trial.id) ?? 0;
                                          onTrialSelect?.(trial, task, {
                                            orderedTrials,
                                            trialIndex: trialIndexInGroup,
                                            trialGroups,
                                          });
                                        }}
                                        className={`relative grid place-items-center gap-0 p-0 leading-none transition-transform hover:-translate-y-px ${STATUS_GLYPH_BOX} ${config.matrixClass} ${isPartial ? "font-mono text-[9.5px] font-semibold tabular-nums tracking-[-0.02em]" : ""}`}
                                        style={getRewardStyle(trial.reward)}
                                        aria-label={`Trial ${trialIndex + 1} ${config.shortLabel}`}
                                        title={fullTitle}
                                      >
                                        {isPartial ? (
                                          partialLabel
                                        ) : (
                                          <StatusIcon status={status} />
                                        )}
                                      </Button>
                                      {analysisIndicator && (
                                        <span
                                          aria-hidden="true"
                                          className={`pointer-events-none absolute -right-[1px] -top-[1px] h-[4px] w-[4px] rounded-full ring-[1px] ring-[color:var(--paper-surface)] ${analysisIndicator.dotClass} ${analysisIndicator.animate ? "animate-pulse" : ""}`}
                                        />
                                      )}
                                    </span>
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
                      colSpan={Math.max(1, renderedAgents.length + 1)}
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
                      colSpan={Math.max(1, renderedAgents.length + 1)}
                      className="py-8 text-center text-muted-foreground"
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
