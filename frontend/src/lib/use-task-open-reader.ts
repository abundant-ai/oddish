"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { fetcher } from "@/lib/api";
import { buildTaskOpenAgentGroups } from "@/lib/task-open-agent-groups";
import { taskDetailKey } from "@/lib/task-detail-resource";
import {
  isBrowseTaskOpen,
  isTaskOpenKeyForTask,
  taskOpenKey,
  taskOpenValue,
  updateTaskOpenDefault,
  type TaskOpenResource,
} from "@/lib/task-open-resource";
import type {
  Task,
  TaskOpenResponse,
  TaskOpenTrialRef,
  TaskOpenVersionRef,
  Trial,
} from "@/lib/types";

function writeVersionToQuery(
  versionId: string | null,
  defaultId: string | null
) {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  if (versionId == null || versionId === defaultId) {
    url.searchParams.delete("version");
  } else {
    url.searchParams.set("version", versionId);
  }
  window.history.replaceState(window.history.state, "", url.toString());
}

function trialFromOpenRef(
  ref: TaskOpenTrialRef,
  task: TaskOpenResponse["task"]
): Trial {
  return {
    ...ref,
    task_id: task.id,
    task_path: task.task_path,
    attempts: 0,
    max_attempts: 0,
    harbor_stage: null,
    error_message:
      ref.error_kind === "timeout"
        ? "AgentTimeoutError"
        : ref.error_kind
          ? "Trial error"
          : null,
  };
}

function taskFromOpen(open: TaskOpenResponse): Task {
  const selected = open.selected_version;
  const completed = selected?.completed_count ?? 0;
  const failed = selected?.failed_count ?? 0;
  const skipped = selected?.skipped_count ?? 0;
  const total = selected?.trial_count ?? 0;
  return {
    ...open.task,
    experiment_id: "",
    experiment_name: "",
    verdict: open.task.verdict
      ? {
          ...open.task.verdict,
          confidence: open.task.verdict.confidence ?? null,
        }
      : null,
    experiment_is_public: false,
    current_version: open.default_version?.version ?? null,
    current_version_id: open.default_version?.id ?? null,
    trial_version: selected?.version ?? null,
    trial_version_id: selected?.id ?? null,
    total,
    completed,
    failed,
    skipped,
    progress: `${completed + failed + skipped}/${total} finished`,
    reward_success: selected?.pass_count ?? 0,
    reward_sum: selected?.reward_sum ?? 0,
    reward_total: selected?.reward_total ?? 0,
    trials: open.trials.map((trial) => trialFromOpenRef(trial, open.task)),
  };
}

export function normalizedAgentModel(
  value: Pick<Trial, "agent" | "model" | "is_probe">
): Pick<Trial, "agent" | "model" | "is_probe"> {
  return {
    agent: value.agent.trim().toLowerCase(),
    model: value.model?.trim().toLowerCase() ?? null,
    is_probe: value.is_probe,
  };
}

export function useTaskOpenReader(
  taskId: string,
  initialVersionId?: string | null
) {
  const { cache, mutate: mutateCache } = useSWRConfig();
  const skipNextDefaultRevalidationRef = useRef(false);
  const [requestedVersionId, setRequestedVersionId] = useState<string | null>(
    () => initialVersionId ?? null
  );
  const openKey = taskOpenKey(taskId, requestedVersionId);
  const {
    data: openResource,
    error,
    isLoading,
    mutate,
  } = useSWR<TaskOpenResource>(openKey, fetcher, {
    refreshInterval: (latestResource) => {
      const latest = taskOpenValue(latestResource);
      return (latest?.selected_version?.pending_count ?? 0) > 0 ||
        ["running", "analyzing", "verdict_pending"].includes(
          latest?.task.status ?? ""
        ) ||
        latest?.task.verdict_status === "queued" ||
        latest?.task.verdict_status === "running"
        ? 30000
        : 0;
    },
    revalidateOnFocus: false,
    keepPreviousData: true,
    revalidateOnMount: !(
      requestedVersionId === null && skipNextDefaultRevalidationRef.current
    ),
    revalidateIfStale: !(
      requestedVersionId === null && skipNextDefaultRevalidationRef.current
    ),
  });
  const open = taskOpenValue(openResource) ?? null;
  const isBrowseSnapshot = isBrowseTaskOpen(openResource);
  const task = useMemo(() => (open ? taskFromOpen(open) : null), [open]);
  const totals = isBrowseSnapshot ? undefined : open?.totals;
  const selectedVersion = open?.selected_version ?? undefined;
  const selectedVersionId = selectedVersion?.id ?? null;
  const defaultVersionId = open?.default_version?.id ?? null;

  useEffect(() => {
    if (
      requestedVersionId === null &&
      skipNextDefaultRevalidationRef.current &&
      openResource
    ) {
      skipNextDefaultRevalidationRef.current = false;
    }
  }, [openResource, requestedVersionId]);

  const explicitVersionMissing =
    requestedVersionId !== null &&
    (error as (Error & { status?: number }) | undefined)?.status === 404;
  const recoveryVersionId = explicitVersionMissing ? requestedVersionId : null;
  const { data: recoveryOpen, error: recoveryError } = useSWR<TaskOpenResponse>(
    recoveryVersionId
      ? ["task-open-default-proof", taskId, recoveryVersionId]
      : null,
    () => fetcher<TaskOpenResponse>(taskOpenKey(taskId)),
    {
      revalidateOnFocus: false,
      onSuccess: (provedOpen) => {
        void mutateCache(taskOpenKey(taskId), provedOpen, {
          revalidate: false,
        });
      },
    }
  );
  useEffect(() => {
    if (
      recoveryVersionId === null ||
      requestedVersionId !== recoveryVersionId ||
      recoveryError ||
      !recoveryOpen
    ) {
      return;
    }
    skipNextDefaultRevalidationRef.current = true;
    setRequestedVersionId(null);
    writeVersionToQuery(null, recoveryOpen.default_version?.id ?? null);
  }, [recoveryError, recoveryOpen, recoveryVersionId, requestedVersionId]);

  const [loadVersionHistory, setLoadVersionHistory] = useState(false);
  const { data: versionHistory, mutate: mutateVersionHistory } = useSWR<
    TaskOpenVersionRef[]
  >(
    loadVersionHistory
      ? `/api/tasks/${encodeURIComponent(taskId)}/versions`
      : null,
    fetcher,
    { revalidateOnFocus: false }
  );
  const versions = useMemo(() => {
    const byId = new Map<string, TaskOpenVersionRef>();
    for (const version of versionHistory ?? []) {
      byId.set(version.id, {
        ...version,
        is_current: version.id === defaultVersionId,
      });
    }
    if (open?.default_version) {
      byId.set(open.default_version.id, open.default_version);
    }
    if (selectedVersion) {
      byId.set(selectedVersion.id, selectedVersion);
    }
    return Array.from(byId.values()).sort((a, b) => b.version - a.version);
  }, [
    defaultVersionId,
    open?.default_version,
    selectedVersion,
    versionHistory,
  ]);
  const [isSettingDefaultVersion, setIsSettingDefaultVersion] = useState(false);
  const [defaultVersionError, setDefaultVersionError] = useState<string | null>(
    null
  );

  const handleSelectVersion = useCallback(
    (id: string) => {
      setRequestedVersionId(id === defaultVersionId ? null : id);
      setDefaultVersionError(null);
      writeVersionToQuery(id, defaultVersionId);
    },
    [defaultVersionId]
  );

  const trialsForVersion = useMemo(() => task?.trials ?? [], [task?.trials]);
  const handleSetDefaultVersion = useCallback(async () => {
    if (!task || !open || !selectedVersion || selectedVersion.is_current) {
      return;
    }
    const versionId = selectedVersion.id;
    const versionNumber = selectedVersion.version;
    setIsSettingDefaultVersion(true);
    setDefaultVersionError(null);
    try {
      const response = await fetch(
        `/api/tasks/${encodeURIComponent(task.id)}/versions/${versionNumber}/default`,
        { method: "PUT" }
      );
      if (!response.ok) {
        const responseData = await response.json().catch(() => ({}));
        throw new Error(
          responseData.detail ||
            responseData.error ||
            "Failed to change the default version"
        );
      }
      const nextDefault: TaskOpenVersionRef = {
        id: versionId,
        version: versionNumber,
        message: selectedVersion.message,
        created_at: selectedVersion.created_at,
        is_current: true,
      };
      const nextOpen = taskOpenValue(updateTaskOpenDefault(open, nextDefault))!;
      const matchesTaskOpenKey = (key: unknown) =>
        isTaskOpenKeyForTask(key, task.id);
      await mutateCache(
        matchesTaskOpenKey,
        (current: TaskOpenResource | undefined) =>
          updateTaskOpenDefault(current, nextDefault),
        { revalidate: false }
      );
      // The unversioned resource now selects the new default, so seed it from
      // the active response rather than retaining the old selected version.
      await mutateCache(taskOpenKey(task.id), nextOpen, {
        revalidate: false,
      });
      await mutateVersionHistory(
        (current) =>
          current?.map((version) => ({
            ...version,
            is_current: version.id === versionId,
          })),
        { revalidate: false }
      );
      setRequestedVersionId(null);
      writeVersionToQuery(null, versionId);
      const detailKey = taskDetailKey(task.id);
      if (cache.get(detailKey) !== undefined) {
        void mutateCache(detailKey);
      }
      void mutateCache(matchesTaskOpenKey);
    } catch (caught) {
      setDefaultVersionError(
        caught instanceof Error
          ? caught.message
          : "Failed to change the default version"
      );
    } finally {
      setIsSettingDefaultVersion(false);
    }
  }, [cache, mutateCache, mutateVersionHistory, open, selectedVersion, task]);

  const exactAgentModels = useMemo(
    () => selectedVersion?.agent_models ?? [],
    [selectedVersion]
  );
  const { agentCards, modelScopedAgents, realAgentCount, realTrialCount } =
    useMemo(
      () => buildTaskOpenAgentGroups(exactAgentModels, trialsForVersion),
      [exactAgentModels, trialsForVersion]
    );
  const detailKey = taskDetailKey(taskId);
  const revalidateReaderResources = useCallback(() => {
    void mutate();
    if (cache.get(detailKey) !== undefined) {
      void mutateCache(detailKey);
    }
  }, [cache, detailKey, mutate, mutateCache]);

  return {
    agentCards,
    defaultVersionError,
    defaultVersionId,
    detailKey,
    error,
    exactAgentModels,
    explicitVersionMissing,
    handleSelectVersion,
    handleSetDefaultVersion,
    isBrowseSnapshot,
    isLoading,
    isSettingDefaultVersion,
    loadVersionHistory,
    modelScopedAgents,
    open,
    openResource,
    realAgentCount,
    realTrialCount,
    recoveryError,
    revalidateReaderResources,
    selectedVersion,
    selectedVersionId,
    setLoadVersionHistory,
    task,
    totals,
    trialsForVersion,
    versions,
  };
}
