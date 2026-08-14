export type TrialDetailDefaultTab = "summary" | "trajectory";

/** The drawer's product default; callers still let an explicit URL tab win. */
export function trialDetailDefaultTab(
  showAnalysis: boolean
): TrialDetailDefaultTab {
  return showAnalysis ? "summary" : "trajectory";
}
