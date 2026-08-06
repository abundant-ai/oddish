import { useUser } from "@clerk/nextjs";

// Feature flag: the experimental eval analytics (Pareto frontier, task-solve
// heatmap, agent cards — everything behind the header's "Eval" toggle) are
// visible only to these accounts while they bake. Hardcoded-flag convention,
// like trial-detail-panel's re-run-analysis button. Public share views have
// no signed-in user, so they never qualify.
const EVAL_GRAPHS_USER_ALLOWLIST = new Set(["meji@abundant.ai"]);

export function useEvalGraphsEnabled(): boolean {
  const { user } = useUser();
  const email = user?.primaryEmailAddress?.emailAddress?.toLowerCase();
  return email != null && EVAL_GRAPHS_USER_ALLOWLIST.has(email);
}
