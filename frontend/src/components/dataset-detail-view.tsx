"use client";

import { useDeferredValue, useMemo, useState } from "react";
import { LayoutDashboard, Search, TableProperties } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import type { Task } from "@/lib/types";
import { QueueKeyIcon } from "@/components/queue-key-icon";

type DatasetDetailViewProps = {
  datasetName: string;
  tasks: Task[];
  isLoading: boolean;
  hasError: boolean;
};

type ModelAggregate = {
  key: string;
  model: string;
  queueKey: string | null;
  total: number;
  scored: number;
  pass: number;
  passRate: number | null;
};

type ExplorerView = "overview" | "tasks";

function aggregateModels(tasks: Task[]): ModelAggregate[] {
  const map = new Map<string, Omit<ModelAggregate, "passRate">>();
  for (const task of tasks) {
    for (const trial of task.trials ?? []) {
      const modelName = trial.model || trial.agent;
      const queueKey = trial.provider || "default";
      const key = `${queueKey}:${modelName}`;
      const existing = map.get(key) ?? {
        key,
        model: modelName,
        queueKey: trial.provider ?? null,
        total: 0,
        scored: 0,
        pass: 0,
      };
      existing.total += 1;
      if (trial.reward !== null) {
        existing.scored += 1;
        if (trial.reward === 1) existing.pass += 1;
      }
      map.set(key, existing);
    }
  }

  return Array.from(map.values())
    .map((entry) => ({
      ...entry,
      passRate:
        entry.scored > 0
          ? Number(((entry.pass / entry.scored) * 100).toFixed(1))
          : null,
    }))
    .sort(
      (a, b) => (b.passRate ?? -1) - (a.passRate ?? -1) || b.scored - a.scored,
    );
}

function inferTaskCategory(task: Task): string {
  const meta = task.github_meta ?? {};
  const fromMeta =
    meta.category ||
    meta.task_category ||
    meta.benchmark_category ||
    meta.track_category;
  if (fromMeta) return fromMeta;
  const parts = task.name.split("_");
  if (parts.length >= 2 && /^[A-Za-z]+$/.test(parts[1]))
    return parts[1].toUpperCase();
  return "General";
}

function inferTaskWorld(task: Task): string {
  const meta = task.github_meta ?? {};
  const fromMeta = meta.world || meta.task_world || meta.benchmark_world;
  if (fromMeta) return fromMeta;
  const match = task.name.match(/(World[_-]?\d+)/i);
  if (match) return match[1].replace("_", "");
  return "Unknown";
}

function inferTaskDomain(task: Task, category: string): string {
  const meta = task.github_meta ?? {};
  const fromMeta =
    meta.domain ||
    meta.task_domain ||
    meta.benchmark_domain ||
    meta.track_domain;
  if (fromMeta) return fromMeta;
  const code = category.toUpperCase();
  if (["JS", "AS", "UM", "LAW"].includes(code)) return "Law";
  if (["BW", "SM", "TD", "IB"].includes(code)) return "Investment Banking";
  if (["TK", "PJ", "MC"].includes(code)) return "Management Consulting";
  return "General";
}

function passRateForTask(task: Task): number | null {
  if (!task.reward_total || task.reward_total <= 0) return null;
  return Math.round(((task.reward_success ?? 0) / task.reward_total) * 100);
}

export function DatasetDetailView({
  datasetName,
  tasks,
  isLoading,
  hasError,
}: DatasetDetailViewProps) {
  const [view, setView] = useState<ExplorerView>("overview");
  const [search, setSearch] = useState("");
  const deferredSearch = useDeferredValue(search);

  const taskRows = useMemo(
    () =>
      tasks.map((task) => {
        const category = inferTaskCategory(task);
        const world = inferTaskWorld(task);
        const domain = inferTaskDomain(task, category);
        return {
          task,
          category,
          world,
          domain,
          criteriaCount: task.reward_total ?? task.total,
          passRate: passRateForTask(task),
        };
      }),
    [tasks],
  );

  const summary = useMemo(() => {
    return { taskCount: tasks.length };
  }, [tasks]);

  const modelStats = useMemo(() => aggregateModels(tasks), [tasks]);
  const topModels = modelStats.slice(0, 5);
  const chartRangeMax = 60;
  const chartTicks = [0, 20, 40, 60];

  const filteredTasks = useMemo(() => {
    const query = deferredSearch.trim().toLowerCase();
    if (!query) return taskRows;
    return taskRows.filter((row) =>
      [
        row.task.name,
        row.task.id,
        row.task.user ?? "",
        row.task.github_username ?? "",
        row.domain,
        row.category,
        row.world,
      ]
        .join(" ")
        .toLowerCase()
        .includes(query),
    );
  }, [taskRows, deferredSearch]);

  return (
    <div className="min-h-[calc(100vh-3.5rem)] bg-background text-foreground -mx-4 -my-4">
      {hasError && (
        <div className="px-6 pt-6">
          <Alert variant="destructive">
            <AlertTitle>Failed to load dataset</AlertTitle>
            <AlertDescription>
              The dataset token may be invalid, or this experiment is not
              public.
            </AlertDescription>
          </Alert>
        </div>
      )}

      <div className="grid min-h-[calc(100vh-3.5rem)] grid-cols-1 lg:grid-cols-[250px_minmax(0,1fr)]">
        <aside className="border-r border-border bg-card/40 px-4 py-5">
          <div className="space-y-2 text-sm">
            <button
              type="button"
              onClick={() => setView("overview")}
              className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left transition ${
                view === "overview"
                  ? "bg-secondary text-secondary-foreground"
                  : "text-muted-foreground hover:bg-muted"
              }`}
            >
              <LayoutDashboard className="h-4 w-4" />
              Overview
            </button>
            <button
              type="button"
              onClick={() => setView("tasks")}
              className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left transition ${
                view === "tasks"
                  ? "bg-secondary text-secondary-foreground"
                  : "text-muted-foreground hover:bg-muted"
              }`}
            >
              <TableProperties className="h-4 w-4" />
              Tasks
            </button>
          </div>
        </aside>

        <main className="px-6 py-5">
          {view === "overview" ? (
            <div className="space-y-5">
              <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_480px]">
                <div className="rounded-xl border border-border bg-card p-4">
                  <div className="text-sm">{datasetName}</div>
                  <p className="mt-2 max-w-3xl text-sm text-muted-foreground">
                    Oddish dataset explorer for public benchmarks. Browse tasks,
                    compare model outcomes, and inspect aggregate benchmark
                    signals.
                  </p>
                </div>

                <div className="rounded-xl border border-border bg-card p-4">
                  <div className="mb-3 flex items-center justify-between">
                    <div className="text-xs font-medium text-muted-foreground">
                      Model
                    </div>
                    <div className="text-xs font-medium text-muted-foreground">
                      Score
                    </div>
                  </div>
                  <div className="space-y-3">
                    {topModels.length === 0 ? (
                      <div className="text-sm text-muted-foreground">
                        No model data yet.
                      </div>
                    ) : (
                      topModels.map((model, index) => {
                        const score = model.passRate ?? 0;
                        const widthPct = Math.max(
                          0,
                          Math.min(100, (score / chartRangeMax) * 100),
                        );
                        return (
                          <div key={model.key} className="space-y-1.5">
                            <div className="flex items-center justify-between text-sm">
                              <div className="truncate flex items-center gap-2">
                                <QueueKeyIcon
                                  queueKey={model.queueKey}
                                  model={model.model}
                                  className="text-muted-foreground"
                                  size={14}
                                />
                                <span className="truncate">{model.model}</span>
                              </div>
                              <div className="font-medium">
                                {model.passRate === null
                                  ? "—"
                                  : `${model.passRate}%`}
                              </div>
                            </div>
                            <div className="h-3 rounded-full bg-muted">
                              <div
                                className={`h-full rounded-full ${
                                  index % 2 === 0 ? "bg-primary" : "bg-accent"
                                }`}
                                style={{ width: `${widthPct}%` }}
                              />
                            </div>
                          </div>
                        );
                      })
                    )}
                  </div>

                  <div className="mt-4 border-t border-border pt-3">
                    <div className="relative h-5">
                      {chartTicks.map((tick) => (
                        <span
                          key={tick}
                          className="absolute top-0 -translate-x-1/2 text-[11px] text-muted-foreground"
                          style={{ left: `${(tick / chartRangeMax) * 100}%` }}
                        >
                          {tick}%
                        </span>
                      ))}
                    </div>
                    <div className="mt-3 flex items-center justify-end">
                      <span className="rounded-md bg-secondary px-2.5 py-1 text-xs text-secondary-foreground">
                        Mean Score
                      </span>
                    </div>
                  </div>
                </div>
              </div>

              <div className="grid grid-cols-1 gap-3">
                <div className="rounded-lg border border-border bg-card p-3">
                  <div className="text-xs text-muted-foreground">Tasks</div>
                  <div className="text-lg font-semibold">
                    {summary.taskCount}
                  </div>
                </div>
              </div>
            </div>
          ) : (
            <div className="space-y-4">
              <div className="text-xl font-semibold">Tasks</div>
              <div className="relative max-w-xl">
                <Search className="pointer-events-none absolute left-2 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                  placeholder="Search tasks..."
                  className="h-9 pl-8"
                />
              </div>

              {isLoading ? (
                <div className="text-sm text-muted-foreground">
                  Loading tasks...
                </div>
              ) : (
                <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                  {filteredTasks.map((row) => (
                    <div
                      key={row.task.id}
                      className="rounded-lg border border-border bg-card p-3"
                    >
                      <div className="text-sm font-medium">{row.task.name}</div>
                      <div className="mt-3 flex items-center justify-between text-xs text-muted-foreground">
                        <span>{row.domain}</span>
                        <span>
                          {row.passRate === null
                            ? "—"
                            : `${row.passRate}% pass`}
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {!isLoading && filteredTasks.length === 0 && (
                <div className="rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground">
                  No tasks match the current filters.
                </div>
              )}
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
