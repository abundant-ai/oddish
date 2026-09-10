"use client";
import { ExperimentDetailView } from "@/components/experiment-detail-view";
import { useSearchParams } from "next/navigation";
import type { ExperimentPageSummary, Task } from "@/lib/types";
import { tasks } from "./records";
export function FixtureExperiment() {
  const scenario = useSearchParams().get("scenario");
  const visibleTasks: Task[] = tasks.flatMap((task) => {
    if (scenario === "unreviewed-only")
      return task.id === "unreviewed" || task.id === "stale-review"
        ? [task]
        : [];
    if (scenario === "live-review" && task.id === "unreviewed")
      return [
        {
          ...task,
          trials: [
            {
              ...tasks[0].trials![0],
              task_id: task.id,
              analysis_status: "running",
              analysis: null,
            },
          ],
        },
      ];
    if (scenario === "live-review" && task.id === "stale-review")
      return [
        {
          ...task,
          active_qa_trial: {
            ...tasks[0].trials![0],
            task_id: task.id,
            kind: "qa",
            status: "running",
          },
        },
      ];
    return [task];
  });
  // Stored verdict counters omit live trial analysis and replacement QA.
  const summary: ExperimentPageSummary = {
    task_count: tasks.length,
    trial_count: 5,
    completed: 5,
    failed: 0,
    skipped: 0,
    active: 0,
    reward_sum: 2,
    reward_total: 5,
    pass_count: 2,
    partial_count: 0,
    fail_count: 3,
    harness_error_count: 1,
    average_score: 0.4,
    qa_accepted: 3,
    qa_rejected: 1,
    qa_running: 2,
    qa_failed: 2,
  };
  return (
    <ExperimentDetailView
      experimentId="review-demo"
      tasksForExperiment={visibleTasks}
      pageSummary={summary}
      costTotals={{ status: "idle" }}
      onRetryCostTotals={() => {}}
      isLoading={false}
      pagesComplete
      headerLeft={<h1>Review meaning fixtures</h1>}
    />
  );
}
