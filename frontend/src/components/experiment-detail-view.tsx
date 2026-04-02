"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ExperimentTrialsTable } from "@/components/experiment-trials-table";
import { TrialDetailPanel } from "@/components/trial-detail-panel";
import { TaskFilesPanel } from "@/components/task-files-panel";
import { UnifiedDrawerWrapper } from "@/components/unified-drawer-wrapper";
import type { Task, Trial } from "@/lib/types";
import { Loader2 } from "lucide-react";
import {
  buildExperimentAgentSummaries,
  getExperimentAgentKey,
  type ExperimentAgentSummary,
} from "@/lib/experiment-agent-grouping";

type DrawerMode = "task" | "trial";

type DrawerState = {
  isOpen: boolean;
  mode: DrawerMode;
  task: Task;
  taskIndex: number;
  orderedTasks: Task[];
  trial: Trial | null;
  trialIndex: number | null;
  orderedTrials: Trial[];
  trialGroups: Array<{
    agent: string;
    model: string | null;
    trials: Trial[];
  }>;
} | null;

interface ExperimentDetailViewProps {
  experimentId?: string;
  tasksForExperiment: Task[];
  isLoading: boolean;
  isLoadingTrials?: boolean;
  hasError?: boolean;
  errorTitle?: string;
  errorDescription?: string;
  headerLeft: React.ReactNode;
  headerRight?: React.ReactNode;
  inlineAlert?: React.ReactNode;
  readOnly?: boolean;
  allowRetry?: boolean;
  apiBaseUrl?: string;
  onTaskDelete?: (task: Task) => Promise<void>;
  onRerun?: (taskIds?: string[]) => void;
}

const AGENT_SUMMARY_STORAGE_PREFIX = "oddish:experiment-agent-summaries:";

function getModelScopedAgentsFromSummaries(
  summaries: ExperimentAgentSummary[],
): Set<string> {
  return new Set(
    summaries.filter((summary) => summary.isModelScoped).map((summary) => summary.agent),
  );
}

type ExperimentSummary = {
  rewardSuccess: number;
  rewardTotal: number;
  totalTrials: number;
  completedTrials: number;
  failedTrials: number;
  passCount: number;
  failCount: number;
  harnessErrorCount: number;
  pendingCount: number;
};

function buildExperimentSummary(tasksForExperiment: Task[]): ExperimentSummary {
  let rewardSuccess = 0;
  let rewardTotal = 0;
  let totalTrials = 0;
  let completedTrials = 0;
  let failedTrials = 0;

  let passCount = 0;
  let failCount = 0;
  let harnessErrorCount = 0;
  let pendingCount = 0;

  for (const task of tasksForExperiment) {
    rewardSuccess += task.reward_success ?? 0;
    rewardTotal += task.reward_total ?? 0;
    totalTrials += task.total;
    completedTrials += task.completed;
    failedTrials += task.failed;

    for (const trial of task.trials ?? []) {
      if (trial.status === "success" && trial.reward === 1) {
        passCount++;
      } else if (trial.status === "success" && trial.reward === 0) {
        failCount++;
      } else if (trial.status === "failed") {
        harnessErrorCount++;
      } else {
        pendingCount++;
      }
    }
  }

  return {
    rewardSuccess,
    rewardTotal,
    totalTrials,
    completedTrials,
    failedTrials,
    passCount,
    failCount,
    harnessErrorCount,
    pendingCount,
  };
}

function ExperimentHeaderMeta({
  isLoading,
  isInitialLoading,
  showPassAtK,
  onToggleShowPassAtK,
  headerRight,
}: {
  isLoading: boolean;
  isInitialLoading: boolean;
  showPassAtK: boolean;
  onToggleShowPassAtK: () => void;
  headerRight?: React.ReactNode;
}) {
  return (
    <div className="flex items-center gap-3">
      {isLoading && (
        <div className="inline-flex items-center gap-1.5 rounded-md border border-border/70 bg-muted/50 px-2 py-1 text-xs text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          <span>{isInitialLoading ? "Loading tasks..." : "Refreshing..."}</span>
        </div>
      )}
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={onToggleShowPassAtK}
        className="h-8 text-xs font-medium"
      >
        {showPassAtK ? "Hide graph" : "Show graph"}
      </Button>
      {headerRight}
    </div>
  );
}

function ExperimentSummaryBar({
  taskCount,
  summary,
  isInitialLoading,
}: {
  taskCount: number;
  summary: ExperimentSummary;
  isInitialLoading: boolean;
}) {
  if (isInitialLoading) {
    return (
      <Card className="bg-card/70">
        <div className="flex items-center gap-2 px-3 py-1.5 text-xs text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          Loading experiment summary...
        </div>
      </Card>
    );
  }

  return (
    <Card className="bg-card/70">
      <div className="flex flex-wrap items-center gap-3 px-3 py-1.5 text-xs">
        <div className="text-muted-foreground">{taskCount} tasks</div>
        <div className="text-muted-foreground">•</div>
        <div className="font-mono text-muted-foreground">
          {summary.completedTrials}/{summary.totalTrials} trials
          {summary.failedTrials > 0 && (
            <span className="text-red-400"> ({summary.failedTrials}F)</span>
          )}
        </div>
        <div className="text-muted-foreground">•</div>
        <div className="font-mono text-muted-foreground">
          Pass rate{" "}
          {summary.rewardTotal > 0
            ? `${Math.round((summary.rewardSuccess / summary.rewardTotal) * 100)}%`
            : "—"}
        </div>
        <div className="text-muted-foreground">•</div>
        <div className="flex items-center gap-2 font-mono text-muted-foreground">
          <span className="text-emerald-400">{summary.passCount}✓</span>
          <span className="text-red-400">{summary.failCount}✗</span>
          {summary.harnessErrorCount > 0 && (
            <span className="text-yellow-400">{summary.harnessErrorCount}⊘</span>
          )}
          {summary.pendingCount > 0 && (
            <span className="text-muted-foreground">{summary.pendingCount}◌</span>
          )}
        </div>
      </div>
    </Card>
  );
}

export function ExperimentDetailView({
  experimentId,
  tasksForExperiment,
  isLoading,
  isLoadingTrials = false,
  hasError = false,
  errorTitle = "Failed to load experiment",
  errorDescription = "Check the API connection and try again.",
  headerLeft,
  headerRight,
  inlineAlert,
  readOnly = false,
  allowRetry = true,
  apiBaseUrl = "/api",
  onTaskDelete,
  onRerun,
}: ExperimentDetailViewProps) {
  const searchParams = useSearchParams();
  const [drawerState, setDrawerState] = useState<DrawerState>(null);
  const [showPassAtK, setShowPassAtK] = useState(false);
  const [cachedAgentSummaries, setCachedAgentSummaries] = useState<
    ExperimentAgentSummary[]
  >([]);
  const hydratedFromUrl = useRef(false);
  const isInitialLoading = isLoading && tasksForExperiment.length === 0;
  const agentSummaryStorageKey = experimentId
    ? `${AGENT_SUMMARY_STORAGE_PREFIX}${experimentId}`
    : null;
  const { agentSummaries, modelScopedAgents } = useMemo(
    () => buildExperimentAgentSummaries(tasksForExperiment),
    [tasksForExperiment],
  );
  const displayAgentSummaries =
    agentSummaries.length > 0 ? agentSummaries : cachedAgentSummaries;
  const displayModelScopedAgents = useMemo(
    () =>
      agentSummaries.length > 0
        ? modelScopedAgents
        : getModelScopedAgentsFromSummaries(cachedAgentSummaries),
    [agentSummaries, modelScopedAgents, cachedAgentSummaries],
  );

  useEffect(() => {
    if (!agentSummaryStorageKey) {
      setCachedAgentSummaries([]);
      return;
    }

    try {
      const raw = window.sessionStorage.getItem(agentSummaryStorageKey);
      if (!raw) {
        setCachedAgentSummaries([]);
        return;
      }
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        setCachedAgentSummaries(parsed as ExperimentAgentSummary[]);
      }
    } catch {
      setCachedAgentSummaries([]);
    }
  }, [agentSummaryStorageKey]);

  useEffect(() => {
    if (!agentSummaryStorageKey || agentSummaries.length === 0) return;
    setCachedAgentSummaries(agentSummaries);
    try {
      window.sessionStorage.setItem(
        agentSummaryStorageKey,
        JSON.stringify(agentSummaries),
      );
    } catch {
      // Ignore storage failures; the live data still drives the table.
    }
  }, [agentSummaryStorageKey, agentSummaries]);

  const buildTrialGroups = useCallback(
    (task: Task) => {
      const trialGroups: Array<{
        agent: string;
        model: string | null;
        trials: Trial[];
      }> = [];
      const trialsByAgent = new Map<string, Trial[]>();
      for (const trial of task.trials ?? []) {
        const key = getExperimentAgentKey(trial, displayModelScopedAgents);
        const existing = trialsByAgent.get(key) ?? [];
        existing.push(trial);
        trialsByAgent.set(key, existing);
      }
      for (const [key, trials] of trialsByAgent) {
        const model = trials.find((t) => t.model)?.model ?? null;
        trialGroups.push({
          agent: key,
          model,
          trials,
        });
      }
      const orderedTrials: Trial[] = [];
      for (const group of trialGroups) {
        orderedTrials.push(...group.trials);
      }
      return { trialGroups, orderedTrials };
    },
    [displayModelScopedAgents],
  );

  useEffect(() => {
    if (!hydratedFromUrl.current) return;

    const next = new URLSearchParams(searchParams.toString());
    if (drawerState?.isOpen) {
      next.set("task", drawerState.task.id);
      if (drawerState.mode === "trial" && drawerState.trial) {
        next.set("trial", drawerState.trial.id);
      } else {
        next.delete("trial");
        next.delete("tab");
        next.delete("file");
      }
    } else {
      next.delete("task");
      next.delete("trial");
      next.delete("tab");
      next.delete("file");
    }

    if (next.toString() !== searchParams.toString()) {
      const url = `${window.location.pathname}${next.toString() ? `?${next.toString()}` : ""}`;
      // Keep URL query in sync without triggering app-router navigation work.
      window.history.replaceState(window.history.state, "", url);
    }
  }, [drawerState, searchParams]);

  useEffect(() => {
    if (hydratedFromUrl.current || tasksForExperiment.length === 0) return;
    hydratedFromUrl.current = true;

    const urlTaskId = searchParams.get("task");
    const urlTrialId = searchParams.get("trial");
    if (!urlTaskId) return;

    const task = tasksForExperiment.find((t) => t.id === urlTaskId);
    if (!task) return;

    const taskIndex = tasksForExperiment.indexOf(task);
    const { trialGroups, orderedTrials } = buildTrialGroups(task);

    if (urlTrialId) {
      const trial = orderedTrials.find((t) => t.id === urlTrialId) ?? null;
      if (trial) {
        const trialIndex = orderedTrials.indexOf(trial);
        setDrawerState({
          isOpen: true,
          mode: "trial",
          task,
          taskIndex,
          orderedTasks: tasksForExperiment,
          trial,
          trialIndex,
          orderedTrials,
          trialGroups,
        });
        return;
      }
    }

    setDrawerState({
      isOpen: true,
      mode: "task",
      task,
      taskIndex,
      orderedTasks: tasksForExperiment,
      trial: null,
      trialIndex: null,
      orderedTrials,
      trialGroups,
    });
  }, [tasksForExperiment, searchParams, buildTrialGroups]);

  const summary = useMemo(
    () => buildExperimentSummary(tasksForExperiment),
    [tasksForExperiment],
  );

  const closeDrawer = () => {
    setDrawerState(null);
  };

  const handleNavigateToFirstTrial = () => {
    if (!drawerState) return;
    const firstGroup = drawerState.trialGroups[0];
    if (!firstGroup || firstGroup.trials.length === 0) return;

    const firstTrial = firstGroup.trials[0];
    setDrawerState({
      ...drawerState,
      mode: "trial",
      trial: firstTrial,
      trialIndex: 0,
    });
  };

  const handleNavigateToTask = () => {
    if (!drawerState) return;
    setDrawerState({
      ...drawerState,
      mode: "task",
      trial: null,
      trialIndex: null,
    });
  };

  const handleNavigateToTrial = (trial: Trial, trialIndex: number) => {
    if (!drawerState) return;
    setDrawerState({
      ...drawerState,
      mode: "trial",
      trial,
      trialIndex,
    });
  };

  return (
    <>
      <Card>
        <CardHeader className="py-3">
          <div className="flex flex-col gap-3">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="flex min-w-0 flex-1 flex-wrap items-center gap-3">
                {headerLeft}
                <ExperimentSummaryBar
                  taskCount={tasksForExperiment.length}
                  summary={summary}
                  isInitialLoading={isInitialLoading}
                />
              </div>
              <ExperimentHeaderMeta
                isLoading={isLoading}
                isInitialLoading={isInitialLoading}
                showPassAtK={showPassAtK}
                onToggleShowPassAtK={() => setShowPassAtK((prev) => !prev)}
                headerRight={headerRight}
              />
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {hasError ? (
            <Alert variant="destructive">
              <AlertTitle>{errorTitle}</AlertTitle>
              <AlertDescription>{errorDescription}</AlertDescription>
            </Alert>
          ) : (
            <div className="space-y-3">
              {inlineAlert}
              <ExperimentTrialsTable
                tasks={tasksForExperiment}
                agentSummaries={displayAgentSummaries}
                modelScopedAgents={displayModelScopedAgents}
                isLoading={isLoading}
                isLoadingTrials={isLoadingTrials}
                showPassAtK={showPassAtK}
                onTaskDelete={onTaskDelete}
                onRerun={onRerun}
                allowRerun={allowRetry}
                readOnly={readOnly}
                onTrialSelect={(trial, task, context) => {
                  const taskIndex = tasksForExperiment.findIndex(
                    (t) => t.id === task.id,
                  );
                  setDrawerState({
                    isOpen: true,
                    mode: "trial",
                    task,
                    taskIndex: taskIndex >= 0 ? taskIndex : 0,
                    orderedTasks: tasksForExperiment,
                    trial,
                    trialIndex: context.trialIndex,
                    orderedTrials: context.orderedTrials,
                    trialGroups: context.trialGroups,
                  });
                }}
                onTaskSelect={(task, context) => {
                  const { trialGroups, orderedTrials } = buildTrialGroups(task);
                  setDrawerState({
                    isOpen: true,
                    mode: "task",
                    task,
                    taskIndex: context.taskIndex,
                    orderedTasks: context.orderedTasks,
                    trial: null,
                    trialIndex: null,
                    orderedTrials,
                    trialGroups,
                  });
                }}
              />
            </div>
          )}
        </CardContent>
      </Card>

      {drawerState && (
        <UnifiedDrawerWrapper
          open={drawerState.isOpen}
          onOpenChange={(open) => !open && closeDrawer()}
          mode={drawerState.mode}
          taskContent={
            <TaskFilesPanel
              isOpen={true}
              onClose={closeDrawer}
              taskId={drawerState.task.id}
              task={drawerState.task}
              orderedTasks={drawerState.orderedTasks}
              taskIndex={drawerState.taskIndex}
              onRetryComplete={onRerun}
              allowRetry={allowRetry}
              onNavigate={(nextTask, nextIndex) => {
                if (!drawerState) return;
                const { trialGroups, orderedTrials } =
                  buildTrialGroups(nextTask);
                setDrawerState({
                  ...drawerState,
                  task: nextTask,
                  taskIndex: nextIndex,
                  orderedTrials,
                  trialGroups,
                });
              }}
              onNavigateToFirstTrial={
                drawerState.trialGroups.length > 0
                  ? handleNavigateToFirstTrial
                  : undefined
              }
              apiBaseUrl={apiBaseUrl}
              contentOnly={true}
            />
          }
          trialContent={
            drawerState.trial && (
              <TrialDetailPanel
                isOpen={true}
                onClose={closeDrawer}
                trial={drawerState.trial}
                task={drawerState.task}
                orderedTrials={drawerState.orderedTrials}
                trialIndex={drawerState.trialIndex}
                trialGroups={drawerState.trialGroups}
                onNavigate={handleNavigateToTrial}
                onNavigateToTask={handleNavigateToTask}
                onRetry={onRerun}
                allowRetry={allowRetry}
                apiBaseUrl={apiBaseUrl}
                contentOnly={true}
              />
            )
          }
        />
      )}
    </>
  );
}
