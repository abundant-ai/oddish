import type { ExperimentQaItemContent } from "@/lib/types";

export type ExperimentQaSignal =
  | "valid"
  | "needs_work"
  | "false_positive"
  | "harness"
  | "running"
  | "unknown";

export const EXPERIMENT_QA_SOURCE_LABEL = {
  pre_trial: "Task review",
  verdict: "Task verdict",
  trial_analysis: "Trial review",
} as const;

export function experimentQaSignal(
  item: Pick<ExperimentQaItemContent, "outcome" | "tier" | "source_type">
): ExperimentQaSignal {
  const outcome = item.outcome?.trim().toUpperCase().replace(/[ -]+/g, "_");
  if (
    outcome === "GOOD_SUCCESS" ||
    outcome === "GOOD_FAILURE" ||
    outcome === "VALID" ||
    outcome === "ACCEPT" ||
    outcome === "PASS" ||
    outcome === "PASSED"
  ) {
    return "valid";
  }
  if (
    outcome === "BAD_SUCCESS" ||
    outcome === "FALSE_POSITIVE" ||
    outcome === "REWARD_HACK"
  ) {
    return "false_positive";
  }
  if (
    outcome === "HARNESS_ERROR" ||
    outcome === "HARNESS" ||
    outcome === "INFRA_ERROR"
  ) {
    return "harness";
  }
  if (outcome === "QUEUED" || outcome === "RUNNING") return "running";
  if (
    outcome === "BAD_FAILURE" ||
    outcome === "NEEDS_WORK" ||
    outcome === "REJECT" ||
    outcome === "FAIL" ||
    outcome === "FAILED" ||
    item.tier === "must_fix" ||
    item.tier === "should_fix"
  ) {
    return "needs_work";
  }
  return "unknown";
}

export function buildPublicQaHref(
  experimentToken: string,
  qaToken: string
): string {
  return `/share/${encodeURIComponent(experimentToken)}/qa?t=${encodeURIComponent(qaToken)}`;
}

export function experimentQaFormField(
  scope: "task" | "item",
  id: string,
  field: string
): string {
  return `${scope}.${encodeURIComponent(id)}.${field}`;
}
