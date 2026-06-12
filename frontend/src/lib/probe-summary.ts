// Shared types + pure helpers for rendering probe runs. Used by the probe
// detail page and the task-drawer PROBE summary so both stay consistent.

export type ProbeMetric = "ratio" | "result_focus" | "none";

export type Attempt = {
  title?: string;
  rationale?: string;
  outcome?: string;
  success?: boolean | null;
  step_indices?: number[];
};

export type ToolInsight = {
  name?: string;
  kind?: "skill" | "mcp";
  note?: string;
};

export type Recommendation = {
  priority: "must_fix" | "should_fix" | "optional";
  action: string;
  rationale?: string;
};

export type ProbeSummary = {
  kind?: string;
  headline?: string;
  summary?: string;
  key_actions?: string[];
  recommendations?: Recommendation[];
  cheating_attempted?: boolean | null;
  cheating_succeeded?: boolean | null;
  evidence?: string;
  attempts?: Attempt[];
  tool_insights?: ToolInsight[];
  model?: string;
  generated_at?: string;
  result_focus_question?: string | null;
  result_focus_findings?: string | null;
};

export type ProbeHarborConfig = {
  mode?: string;
  extra_instructions?: string;
  // "cheat_ratio" kept as a legacy alias — normalizeMetric folds it to "ratio".
  evaluation_metric?: "ratio" | "result_focus" | "none" | "cheat_ratio";
  ratio_unit?: string | null;
  ratio_verb?: string | null;
} | null;

// A probe run is a trial row; this is the subset the probe UIs read.
export type ProbeTrial = {
  id: string;
  agent: string;
  model: string | null;
  status: string;
  reward: number | null;
  created_at?: string;
  harbor_config: ProbeHarborConfig;
  result: { _artifacts?: unknown } | null;
  analysis: ProbeSummary | null;
  analysis_status: string | null;
  analysis_error: string | null;
  error_message?: string | null;
};

export function pluralize(noun: string): string {
  const n = noun.trim();
  if (!n) return "";
  // Operator-supplied units are often already plural ("issues", "results").
  // Treat a trailing "s" as already plural so we don't produce "issueses".
  if (/s$/.test(n)) return n;
  if (/[xz]$|[cs]h$/.test(n)) return n + "es";
  if (/[^aeiou]y$/.test(n)) return n.slice(0, -1) + "ies";
  return n + "s";
}

export function normalizeMetric(raw: string | null | undefined): ProbeMetric {
  const m = raw ?? "none";
  const mapped = m === "cheat_ratio" ? "ratio" : m;
  const known: ProbeMetric[] = ["ratio", "result_focus", "none"];
  return known.includes(mapped as ProbeMetric) ? (mapped as ProbeMetric) : "none";
}

export function ratioUnitVerb(cfg: ProbeHarborConfig): {
  unit: string;
  verb: string | null;
} {
  const raw = cfg?.evaluation_metric ?? "none";
  const unit = cfg?.ratio_unit ?? (raw === "cheat_ratio" ? "cheat" : "attempt");
  const verb = cfg?.ratio_verb ?? (raw === "cheat_ratio" ? "succeeded" : null);
  return { unit, verb };
}

export type AttemptTally = {
  succeeded: number;
  blocked: number;
  investigation: number;
  cheatTotal: number;
};

export function tallyAttempts(attempts: Attempt[] | undefined): AttemptTally {
  const all = attempts ?? [];
  const succeeded = all.filter((a) => a.success === true).length;
  const blocked = all.filter((a) => a.success === false).length;
  const investigation = all.length - succeeded - blocked;
  return { succeeded, blocked, investigation, cheatTotal: succeeded + blocked };
}

export function isTerminalProbeStatus(status: string): boolean {
  return status === "success" || status === "failed";
}
