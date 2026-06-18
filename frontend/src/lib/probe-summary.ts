// Shared types + pure helpers for rendering probe runs. Used by the probe
// detail page and the task-drawer PROBE summary so both stay consistent.

type ProbeMetric = "result_focus" | "none";

type ToolInsight = {
  name?: string;
  kind?: "skill" | "mcp";
  note?: string;
};

type Recommendation = {
  priority: "must_fix" | "should_fix" | "optional";
  action: string;
  rationale?: string;
};

const PRIORITY_ORDER: Record<string, number> = {
  must_fix: 0,
  should_fix: 1,
  optional: 2,
};

export const PRIORITY_META: Record<string, { label: string; cls: string }> = {
  must_fix: { label: "Must fix", cls: "bg-red-500/15 text-red-600" },
  should_fix: { label: "Should fix", cls: "bg-amber-500/15 text-amber-700" },
  optional: { label: "Optional", cls: "bg-slate-500/15 text-slate-600" },
};

// Recommendations sorted highest-priority first; unknown priorities sink last.
export function sortRecommendations(
  recs: Recommendation[] | undefined,
): Recommendation[] {
  return [...(recs ?? [])].sort(
    (a, b) =>
      (PRIORITY_ORDER[a.priority] ?? 9) - (PRIORITY_ORDER[b.priority] ?? 9),
  );
}

export type ProbeSummary = {
  kind?: string;
  headline?: string;
  summary?: string;
  key_actions?: string[];
  recommendations?: Recommendation[];
  evidence?: string;
  tool_insights?: ToolInsight[];
  model?: string;
  generated_at?: string;
  result_focus_question?: string | null;
  // Plain-language recap of result_focus_findings (the JSON answer); shown as the
  // result-focus body, with the structured findings behind a toggle.
  result_focus_summary?: string | null;
  result_focus_findings?: string | Record<string, unknown> | unknown[] | null;
};

type ProbeHarborConfig = {
  mode?: string;
  extra_instructions?: string;
  // Operator-selected preset name; absent on older / preset-less runs.
  probe_name?: string | null;
  // "cheat_ratio"/"ratio" kept as legacy aliases — normalizeMetric maps them to "none".
  evaluation_metric?: "result_focus" | "none" | "cheat_ratio" | "ratio";
} | null;

// A probe run is a trial row; this is the subset the probe UIs read.
export type ProbeTrial = {
  id: string;
  agent: string;
  model: string | null;
  status: string;
  reward: number | null;
  created_at?: string;
  task_version_id?: string | null;
  harbor_config: ProbeHarborConfig;
  result: { _artifacts?: unknown } | null;
  analysis: ProbeSummary | null;
  analysis_status: string | null;
  analysis_error: string | null;
  error_message?: string | null;
};

export function normalizeMetric(raw: string | null | undefined): ProbeMetric {
  const m = raw ?? "none";
  if (m === "result_focus") return "result_focus";
  return "none";
}

export function isTerminalProbeStatus(status: string): boolean {
  return status === "success" || status === "failed";
}
