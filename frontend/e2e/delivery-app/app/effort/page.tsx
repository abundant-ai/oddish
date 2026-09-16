"use client";

import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { ExperimentDetailView } from "../../../../src/components/experiment-detail-view";
import { ExperimentTrialsTable } from "../../../../src/components/experiment-trials-table";
import { ProbeSubmitForm } from "../../../../src/components/probe-submit-form";
import { ExperimentRunDialog } from "../../../../src/components/experiment-run-dialog";
import { buildExperimentAgentSummaries } from "../../../../src/lib/experiment-agent-grouping";
import type { Task, Trial } from "../../../../src/lib/types";

const model = "global.anthropic.claude-opus-5";
const tasks: Task[] = ["repair-queue", "repair-writes", "repair-timeout"].map(
  (id) => {
    const trials: Trial[] = ["low", "medium", "high", "xhigh"].flatMap(
      (reasoning_effort, effortIndex) =>
        Array.from({ length: 5 }, (_, i) => ({
          id: `${id}-${reasoning_effort}-${i}`,
          name: `${id}-${reasoning_effort}-${i}`,
          task_id: id,
          task_path: id,
          agent: "claude-code",
          model,
          reasoning_effort,
          provider: "bedrock",
          queue_key: model,
          kind: "agent",
          status: "success",
          reward: (i + effortIndex) % 3 === 0 ? 0 : 1,
          attempts: 1,
          max_attempts: 1,
          harbor_stage: "completed",
          created_at: "2026-09-14T10:00:00Z",
        }))
    );
    return {
      id,
      name: id,
      task_path: id,
      user: "test",
      status: "completed",
      priority: "low",
      experiment_id: "effort-preview",
      total: 20,
      completed: 20,
      failed: 0,
      trials,
      created_at: "2026-09-14T10:00:00Z",
      updated_at: "2026-09-14T10:00:00Z",
    };
  }
);

function EffortComparison() {
  const params = useSearchParams();
  const groupEfforts = params.get("groupEfforts") === "1";
  const displayedTasks =
    params.get("sample") === "mixed"
      ? [
          {
            ...tasks[0],
            total: 5,
            completed: 5,
            trials: tasks[0].trials!.slice(0, 5).map((trial, i) => ({
              ...trial,
              reasoning_effort: i < 2 ? null : "high",
              reward: i === 4 ? 1 : 0,
            })),
          },
        ]
      : tasks;
  const [selected, setSelected] = useState("");
  if (params.has("detail"))
    return (
      <ExperimentDetailView
        tasksForExperiment={displayedTasks}
        costTotals={{ status: "idle" }}
        onRetryCostTotals={() => {}}
        isLoading={false}
        headerLeft={<h1>Effort comparison</h1>}
        readOnly={params.has("public")}
        allowRetry={false}
        showAnalysis={false}
      />
    );
  return (
    <Suspense>
      <div className="space-y-5">
        <div className="flex items-center justify-between gap-4">
          <h1 className="font-mono text-xl">Reasoning effort comparison</h1>
          <ExperimentRunDialog
            experimentId="effort-preview"
            tasks={displayedTasks}
            disabled={false}
          />
        </div>
        <ExperimentTrialsTable
          experimentId="effort-preview"
          tasks={displayedTasks}
          agentSummaries={buildExperimentAgentSummaries(
            displayedTasks,
            groupEfforts
          )}
          groupEfforts={groupEfforts}
          isLoading={false}
          pagesComplete
          showPassAtK
          showAnalysis={false}
          onTrialSelect={(trial, _task, context) =>
            setSelected(
              `${trial.reasoning_effort}: ${context.trialGroups.find((group) => group.trials.some((t) => t.id === trial.id))?.trials.length} trials`
            )
          }
        />
        <section aria-label="Probe launch">
          <ProbeSubmitForm taskId="repair-queue" />
        </section>
        <output aria-label="Selected effort">{selected}</output>
      </div>
    </Suspense>
  );
}

export default function Page() {
  return (
    <Suspense>
      <EffortComparison />
    </Suspense>
  );
}
