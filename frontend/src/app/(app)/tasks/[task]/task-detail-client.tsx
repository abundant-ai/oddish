"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import useSWR from "swr";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { fetcher } from "@/lib/api";
import { formatRelativeTime, formatShortDateTime } from "@/lib/utils";
import type { EvidenceCell, Task, TaskVersion } from "@/lib/types";
import { TaskFilesPanel } from "@/components/task-files-panel";
import { TrialInspectDrawer } from "@/components/trial-inspect-drawer";
import { ArrowLeft, Beaker, GitBranch } from "lucide-react";

function formatMeanReward(value: number | null | undefined) {
  if (value == null) return "—";
  return `${Math.round(value * 100)}%`;
}

function versionLabel(version: TaskVersion) {
  const hash = version.content_hash ? version.content_hash.slice(0, 10) : null;
  return `v${version.version}${hash ? ` · ${hash}` : ""}`;
}

type TaskDetailClientProps = {
  taskId: string;
  task: Task | null;
  versions: TaskVersion[];
  selectedVersion: number | null;
  initialEvidence: EvidenceCell[];
};

export function TaskDetailClient({
  taskId,
  task,
  versions,
  selectedVersion,
  initialEvidence,
}: TaskDetailClientProps) {
  const router = useRouter();
  const [inspecting, setInspecting] = useState<{
    trialId: string;
    taskId: string;
  } | null>(null);
  const evidenceKey =
    selectedVersion == null
      ? null
      : `/api/tasks/${encodeURIComponent(taskId)}/versions/${selectedVersion}/evidence`;
  const { data: evidence = initialEvidence, error } = useSWR<EvidenceCell[]>(
    evidenceKey,
    fetcher,
    {
      fallbackData: initialEvidence,
      refreshInterval: 30000,
      revalidateOnFocus: false,
    }
  );
  const selectedVersionRow =
    versions.find((version) => version.version === selectedVersion) ?? null;
  const { data: taskWithTrials } = useSWR<Task>(
    task
      ? `/api/tasks/${encodeURIComponent(taskId)}?include_trials=true`
      : null,
    fetcher,
    {
      refreshInterval: 30000,
      revalidateOnFocus: false,
    }
  );
  const trials = useMemo(
    () => taskWithTrials?.trials ?? [],
    [taskWithTrials?.trials]
  );
  const trialsForVersion = useMemo(() => {
    if (!selectedVersionRow) return [];
    return trials
      .filter((trial) => trial.task_version_id === selectedVersionRow.id)
      .sort((a, b) =>
        (b.finished_at ?? b.created_at).localeCompare(
          a.finished_at ?? a.created_at
        )
      );
  }, [selectedVersionRow, trials]);

  if (!task) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Task not found</AlertTitle>
        <AlertDescription>
          The task could not be loaded, or you do not have access to it.
        </AlertDescription>
      </Alert>
    );
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-2">
          <Button variant="ghost" size="sm" asChild className="-ml-2 h-8">
            <Link href="/tasks">
              <ArrowLeft className="mr-1.5 h-4 w-4" />
              Tasks
            </Link>
          </Button>
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="font-mono text-2xl font-semibold tracking-tight">
              {task.name}
            </h1>
            {selectedVersion != null ? (
              <Badge variant="outline" className="font-mono">
                v{selectedVersion}
              </Badge>
            ) : null}
          </div>
          <p className="text-muted-foreground max-w-3xl text-sm">
            Task versions are immutable. Evidence below is pooled by the pinned
            task version and agent identity, independent of which experiment or
            job produced it.
          </p>
        </div>
        <Button asChild>
          <Link href="/experiments/new">
            <Beaker className="mr-2 h-4 w-4" />
            Build experiment
          </Link>
        </Button>
      </div>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
        <Card className="border-[#6f88b4]/20 shadow-xs">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <GitBranch className="text-muted-foreground h-4 w-4" />
              Version
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <Select
              value={selectedVersion == null ? "" : String(selectedVersion)}
              onValueChange={(value) => {
                router.replace(
                  `/tasks/${encodeURIComponent(taskId)}?version=${value}`
                );
              }}
            >
              <SelectTrigger className="border-[#6f88b4]/20">
                <SelectValue placeholder="Select task version" />
              </SelectTrigger>
              <SelectContent>
                {versions.map((version) => (
                  <SelectItem key={version.id} value={String(version.version)}>
                    {versionLabel(version)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            {selectedVersionRow ? (
              <div className="border-border/70 bg-muted/25 space-y-3 rounded-lg border p-3 text-sm">
                <div>
                  <div className="text-muted-foreground text-[11px] tracking-wide uppercase">
                    Content hash
                  </div>
                  <div className="mt-1 font-mono text-xs break-all">
                    {selectedVersionRow.content_hash ?? "—"}
                  </div>
                </div>
                <div>
                  <div className="text-muted-foreground text-[11px] tracking-wide uppercase">
                    Bundle
                  </div>
                  <div className="mt-1 font-mono text-xs break-all">
                    {selectedVersionRow.task_s3_key ??
                      selectedVersionRow.task_path}
                  </div>
                </div>
                <div>
                  <div className="text-muted-foreground text-[11px] tracking-wide uppercase">
                    Message
                  </div>
                  <div className="mt-1 text-xs">
                    {selectedVersionRow.message || "No version message."}
                  </div>
                </div>
                <div className="text-muted-foreground text-xs">
                  Created {formatShortDateTime(selectedVersionRow.created_at)}
                </div>
              </div>
            ) : (
              <div className="border-border/70 text-muted-foreground rounded-lg border border-dashed p-4 text-sm">
                No task versions have been registered yet.
              </div>
            )}
          </CardContent>
        </Card>

        <Card className="border-[#6f88b4]/20 shadow-xs">
          <CardHeader className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
            <CardTitle className="text-base">Evidence Matrix</CardTitle>
            <div className="text-muted-foreground text-xs">
              {evidence.length} agent{evidence.length === 1 ? "" : "s"}
            </div>
          </CardHeader>
          <CardContent>
            {error ? (
              <Alert variant="destructive">
                <AlertTitle>Evidence failed to load</AlertTitle>
                <AlertDescription>
                  Refresh the page or check the backend logs.
                </AlertDescription>
              </Alert>
            ) : evidence.length === 0 ? (
              <div className="bg-card/60 text-muted-foreground rounded-lg border border-dashed border-[#6f88b4]/30 px-6 py-10 text-center text-sm">
                No evidence exists for this task version yet. Create an
                experiment or run a job against this version to populate it.
              </div>
            ) : (
              <div className="border-border/70 overflow-hidden rounded-lg border">
                <div className="bg-muted/40 text-muted-foreground grid grid-cols-[minmax(180px,1.4fr)_90px_110px_130px] px-3 py-2 text-[11px] tracking-wide uppercase">
                  <div>Agent</div>
                  <div>Trials</div>
                  <div>Mean reward</div>
                  <div>Last run</div>
                </div>
                {evidence.map((cell) => (
                  <div
                    key={`${cell.task_version_id}:${cell.agent_equivalence_key}`}
                    className="border-border/70 grid grid-cols-[minmax(180px,1.4fr)_90px_110px_130px] items-center border-t px-3 py-3 text-sm"
                  >
                    <div className="min-w-0">
                      <div className="truncate font-mono font-medium">
                        {cell.harness}
                      </div>
                      <div className="text-muted-foreground truncate text-xs">
                        {cell.provider}/{cell.model}
                      </div>
                    </div>
                    <div className="font-mono">{cell.n_trials}</div>
                    <div className="font-mono">
                      {formatMeanReward(cell.mean_reward)}
                    </div>
                    <div className="text-muted-foreground text-xs">
                      {cell.last_run_at
                        ? formatRelativeTime(cell.last_run_at)
                        : "—"}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card className="border-[#6f88b4]/20 shadow-xs">
          <CardHeader>
            <CardTitle className="text-base">Files</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            <TaskFilesPanel
              isOpen
              onClose={() => {}}
              taskId={taskId}
              task={{
                ...task,
                current_version: selectedVersion,
                current_version_id: selectedVersionRow?.id ?? null,
              }}
              contentOnly
            />
          </CardContent>
        </Card>

        <Card className="border-[#6f88b4]/20 shadow-xs">
          <CardHeader className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
            <CardTitle className="text-base">Trials On This Version</CardTitle>
            <div className="text-muted-foreground text-xs">
              {trialsForVersion.length} trial
              {trialsForVersion.length === 1 ? "" : "s"}
            </div>
          </CardHeader>
          <CardContent>
            {trialsForVersion.length === 0 ? (
              <div className="bg-card/60 text-muted-foreground rounded-lg border border-dashed border-[#6f88b4]/30 px-6 py-10 text-center text-sm">
                No terminal trials exist for this version yet.
              </div>
            ) : (
              <div className="border-border/70 max-h-[70vh] overflow-auto rounded-lg border">
                <table className="min-w-full text-sm">
                  <thead>
                    <tr className="bg-muted/40 text-muted-foreground text-left text-[11px] tracking-wide uppercase">
                      <th className="px-3 py-2 font-medium">Trial</th>
                      <th className="px-3 py-2 font-medium">Agent</th>
                      <th className="px-3 py-2 font-medium">Status</th>
                      <th className="px-3 py-2 font-medium">Reward</th>
                      <th className="px-3 py-2 text-right font-medium">
                        Inspect
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {trialsForVersion.map((trial) => (
                      <tr key={trial.id} className="border-border/70 border-t">
                        <td className="px-3 py-3 font-mono text-xs">
                          {trial.id}
                        </td>
                        <td className="px-3 py-3 font-mono text-xs">
                          {trial.agent}
                          {trial.model ? ` · ${trial.model}` : ""}
                        </td>
                        <td className="px-3 py-3">
                          <Badge variant="outline">{trial.status}</Badge>
                        </td>
                        <td className="px-3 py-3 font-mono">
                          {formatMeanReward(trial.reward)}
                        </td>
                        <td className="px-3 py-3 text-right">
                          <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            className="h-7"
                            onClick={() =>
                              setInspecting({
                                trialId: trial.id,
                                taskId: trial.task_id,
                              })
                            }
                          >
                            Inspect
                          </Button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {inspecting ? (
        <TrialInspectDrawer
          open
          onOpenChange={(open) => {
            if (!open) setInspecting(null);
          }}
          taskId={inspecting.taskId}
          trialId={inspecting.trialId}
          siblingTrialIds={trialsForVersion.map((trial) => trial.id)}
          onTrialChange={(trialId) =>
            setInspecting((current) =>
              current ? { ...current, trialId } : current
            )
          }
          noBackdrop
        />
      ) : null}
    </div>
  );
}
