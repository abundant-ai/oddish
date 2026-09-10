"use client";
import { ExperimentDetailView } from "@/components/experiment-detail-view";
import { tasks } from "./records";
export function FixtureExperiment() {
  return (
    <ExperimentDetailView
      experimentId="review-demo"
      tasksForExperiment={tasks}
      costTotals={{ status: "idle" }}
      onRetryCostTotals={() => {}}
      isLoading={false}
      pagesComplete
      headerLeft={<h1>Review meaning fixtures</h1>}
    />
  );
}
