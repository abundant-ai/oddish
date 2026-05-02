"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { TaskFilesPanel } from "@/components/task-files-panel";
import { TrialInspectDrawer } from "@/components/trial-inspect-drawer";
import { fetcher } from "@/lib/api";
import type { Task, Trial } from "@/lib/types";

interface TaskVersion {
  id: string;
  task_id: string;
  version: number;
  task_path: string;
  task_s3_key: string | null;
  content_hash: string | null;
  message: string | null;
  created_by_user_id: string | null;
  created_at: string;
}

function fmt(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return n.toFixed(2);
}

function rel(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function statusVariant(
  status: string,
): "default" | "secondary" | "outline" | "destructive" {
  if (status === "success") return "secondary";
  if (status === "failed") return "destructive";
  if (status === "running") return "default";
  return "outline";
}

export function TaskDetailClient({ taskId }: { taskId: string }) {
  const encoded = encodeURIComponent(taskId);

  const { data: task, error: taskError } = useSWR<Task>(
    `/api/tasks/${encoded}`,
    fetcher,
    { refreshInterval: 30_000 },
  );
  const { data: versions, error: versionsError } = useSWR<TaskVersion[]>(
    `/api/tasks/${encoded}/versions`,
    fetcher,
  );
  const { data: trials, error: trialsError } = useSWR<Trial[]>(
    `/api/tasks/${encoded}/trials`,
    fetcher,
    { refreshInterval: 30_000 },
  );

  const sortedVersions = useMemo(() => {
    if (!versions) return [];
    return [...versions].sort((a, b) => b.version - a.version);
  }, [versions]);

  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(
    null,
  );
  const activeVersionId =
    selectedVersionId ?? sortedVersions[0]?.id ?? null;
  const activeVersion = sortedVersions.find((v) => v.id === activeVersionId);

  const trialsForVersion = useMemo<Trial[]>(() => {
    if (!trials || !activeVersionId) return [];
    return [...trials]
      .filter((t) => t.task_version_id === activeVersionId)
      .sort((a, b) => {
        const av = a.finished_at ?? a.created_at ?? "";
        const bv = b.finished_at ?? b.created_at ?? "";
        return bv.localeCompare(av);
      });
  }, [trials, activeVersionId]);

  const [inspecting, setInspecting] = useState<{ trialId: string } | null>(
    null,
  );

  if (taskError) {
    return (
      <div className="p-4">
        <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
          Failed to load task: {String((taskError as Error).message)}
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-(--breakpoint-2xl) space-y-4 p-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-mono text-[26px] font-semibold tracking-[-0.02em]">
            {task?.name ?? taskId}
          </h1>
          <p className="text-xs text-muted-foreground">
            {task?.id ?? ""} · {sortedVersions.length} version
            {sortedVersions.length === 1 ? "" : "s"}
          </p>
          {task?.tags && Object.keys(task.tags).length > 0 ? (
            <div className="mt-2 flex flex-wrap gap-1">
              {Object.entries(task.tags)
                .filter(([k]) => !k.startsWith("github_"))
                .map(([k, v]) => (
                  <Link
                    key={k}
                    href={`/tasks?query=${encodeURIComponent(v)}`}
                    title={`Filter by ${k}=${v}`}
                  >
                    <Badge
                      variant="secondary"
                      className="cursor-pointer font-mono text-[10px] hover:bg-muted"
                    >
                      {k}={v}
                    </Badge>
                  </Link>
                ))}
            </div>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          {sortedVersions.length > 0 ? (
            <Select
              value={activeVersionId ?? undefined}
              onValueChange={(v) => setSelectedVersionId(v)}
            >
              <SelectTrigger className="h-9 w-[220px]">
                <SelectValue placeholder="Select version" />
              </SelectTrigger>
              <SelectContent>
                {sortedVersions.map((v) => (
                  <SelectItem key={v.id} value={v.id}>
                    v{v.version}
                    {v.message ? ` — ${v.message.slice(0, 40)}` : ""}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : null}
        </div>
      </div>

      {versionsError ? (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
          Failed to load versions: {String((versionsError as Error).message)}
        </div>
      ) : null}

      {activeVersion ? (
        <div className="rounded-md border bg-card p-4 text-sm">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <div>
              <div className="text-[10px] uppercase text-muted-foreground">
                version
              </div>
              <div className="font-mono">v{activeVersion.version}</div>
            </div>
            <div>
              <div className="text-[10px] uppercase text-muted-foreground">
                content hash
              </div>
              <div className="font-mono text-xs">
                {activeVersion.content_hash
                  ? activeVersion.content_hash.slice(0, 16)
                  : "—"}
              </div>
            </div>
            <div>
              <div className="text-[10px] uppercase text-muted-foreground">
                created
              </div>
              <div>{rel(activeVersion.created_at)}</div>
            </div>
            <div>
              <div className="text-[10px] uppercase text-muted-foreground">
                trials on this version
              </div>
              <div className="font-mono">{trialsForVersion.length}</div>
            </div>
          </div>
          {activeVersion.message ? (
            <div className="mt-3 text-xs text-muted-foreground">
              {activeVersion.message}
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="rounded-md border bg-card">
          <div className="border-b px-4 py-2 text-xs font-medium text-muted-foreground">
            Files
          </div>
          <TaskFilesPanel
            isOpen={true}
            onClose={() => {}}
            taskId={taskId}
            task={task ?? null}
            contentOnly
          />
        </div>

        <div className="rounded-md border">
          <div className="border-b px-4 py-2 text-xs font-medium text-muted-foreground">
            Trials on this version
          </div>
          {trialsError ? (
            <div className="p-3 text-sm">
              Failed to load trials:{" "}
              {String((trialsError as Error).message)}
            </div>
          ) : !trials || !versions ? (
            <Skeleton className="m-3 h-32" />
          ) : trialsForVersion.length === 0 ? (
            <div className="p-6 text-center text-sm text-muted-foreground">
              No trials for this version yet.
            </div>
          ) : (
            <div className="max-h-[70vh] overflow-y-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Agent</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Reward</TableHead>
                    <TableHead>Finished</TableHead>
                    <TableHead className="text-right">Inspect</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {trialsForVersion.map((t) => (
                    <TableRow key={t.id}>
                      <TableCell className="font-mono text-xs">
                        {t.agent}
                        {t.model ? ` · ${t.model}` : ""}
                      </TableCell>
                      <TableCell>
                        <Badge variant={statusVariant(t.status)}>
                          {t.status}
                        </Badge>
                      </TableCell>
                      <TableCell className="font-mono text-xs">
                        {fmt(t.reward)}
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {rel(t.finished_at)}
                      </TableCell>
                      <TableCell className="text-right">
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-7"
                          onClick={() => setInspecting({ trialId: t.id })}
                        >
                          Inspect
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </div>
      </div>

      <div className="flex justify-end">
        <Button variant="ghost" size="sm" asChild>
          <Link href="/tasks">← All tasks</Link>
        </Button>
      </div>

      {inspecting ? (
        <TrialInspectDrawer
          open={true}
          onOpenChange={(o) => {
            if (!o) setInspecting(null);
          }}
          trialId={inspecting.trialId}
          taskId={taskId}
          siblingTrialIds={trialsForVersion.map((t) => t.id)}
          onTrialChange={(nextId) => setInspecting({ trialId: nextId })}
          noBackdrop
        />
      ) : null}
    </div>
  );
}
