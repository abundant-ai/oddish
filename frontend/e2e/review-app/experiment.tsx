"use client";
import { useEffect, useState } from "react";
import { ExperimentDetailView } from "@/components/experiment-detail-view";
import { useSearchParams } from "next/navigation";
import type { ExperimentPageSummary, Task } from "@/lib/types";
import { tasks } from "./records";
import { setFixtureAccount } from "./clerk";
export function FixtureExperiment() {
  const scenario = useSearchParams().get("scenario");
  useEffect(() => {
    if (scenario === "layout-deep-link") setFixtureAccount("alice", "org-a");
  }, [scenario]);
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
  const [expandedRows, setExpandedRows] = useState(false);
  useEffect(() => {
    const expand = () => setExpandedRows(true);
    window.addEventListener("fixture-add-tasks", expand);
    return () => window.removeEventListener("fixture-add-tasks", expand);
  }, []);
  const scrollCount =
    scenario === "scroll-threshold"
      ? expandedRows
        ? 250
        : 199
      : scenario === "scroll-25"
        ? 25
        : scenario === "scroll-250" || scenario === "scroll-restored"
          ? 250
          : 0;
  const displayedTasks = scrollCount
    ? Array.from({ length: scrollCount }, (_, index) => ({
        ...tasks[0],
        id: `scroll-${index}`,
        name: `kafka-consumer-offset-recovery-after-broker-restart-${String(index + 1).padStart(3, "0")}`,
      }))
    : visibleTasks;
  const [refreshing, setRefreshing] = useState(false);

  // Stored verdict counters omit live trial analysis and replacement QA.
  const summary: ExperimentPageSummary = {
    task_count: displayedTasks.length,
    trial_count: scenario === "duplication" ? 8 : 5,
    completed: 5,
    failed: scenario === "duplication" ? 1 : 0,
    skipped: scenario === "duplication" ? 2 : 0,
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
      tasksForExperiment={displayedTasks}
      pageSummary={summary}
      costTotals={
        scenario === "duplication"
          ? {
              status: "ready",
              data: {
                cost_usd: 12.5,
                cost_trial_count: 7,
                cost_has_estimated: false,
                cost_has_native: true,
                token_count: 1500000,
                token_trial_count: 7,
                owned_cost_usd: 8.5,
                owned_trial_count: 5,
                owned_has_estimated: false,
                owned_has_native: true,
                owned_token_count: 1000000,
                owned_token_trial_count: 5,
                billed_cost_usd: 8.5,
                billed_trial_count: 5,
                billed_has_estimated: false,
                billed_has_native: true,
                billed_token_count: 1000000,
                billed_token_trial_count: 5,
                total_trials: 8,
                qa_cost_usd: 0.2,
                owned_qa_cost_usd: 0.1,
              },
            }
          : { status: "idle" }
      }
      onRetryCostTotals={() => {}}
      isLoading={false}
      isLoadingTrials={refreshing}
      pagesComplete
      headerLeft={
        <>
          <h1>Review meaning fixtures</h1>
          {(scenario === "scroll-threshold" ||
            scenario === "scroll-restored") && (
            <div aria-hidden style={{ height: 1800 }} />
          )}
          {scenario === "refresh" && (
            <button onClick={() => setRefreshing((value) => !value)}>
              Toggle background refresh
            </button>
          )}
        </>
      }
    />
  );
}
