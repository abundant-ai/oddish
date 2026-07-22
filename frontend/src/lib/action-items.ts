// Shared types + pure helpers for QA analyzer action items. Mirrors the
// backend pydantic `ActionItem` in oddish/src/oddish/analyze/models.py.

export type ActionItemSource = "pre_trial" | "post_trial";
export type ProblemType = "incompleteness" | "mismatch";
export type Dimension = "verifier" | "oracle" | "info_leakage";
export type ActionTier = "must_fix" | "should_fix" | "optional";

export interface ActionItem {
  id: string;
  source: ActionItemSource;
  problem_type: ProblemType;
  dimension: Dimension;
  file: string;
  line_start: number;
  line_end: number;
  title: string;
  detail: string;
  recommendation: string;
  tier: ActionTier;
  links_to?: string | null;
  exploited?: boolean;
  exploit_evidence?: string | null;
  causal?: boolean;
}

const TIER_ORDER: Record<ActionTier, number> = { must_fix: 0, should_fix: 1, optional: 2 };

export const TIER_META: Record<ActionTier, { label: string; cls: string }> = {
  must_fix: { label: "Must fix", cls: "bg-red-500/15 text-red-600" },
  should_fix: { label: "Should fix", cls: "bg-amber-500/15 text-amber-700" },
  optional: { label: "Optional", cls: "bg-slate-500/15 text-slate-600" },
};

export const DIMENSION_META: Record<Dimension, { label: string }> = {
  verifier: { label: "Verifier completeness" },
  oracle: { label: "Oracle correctness" },
  info_leakage: { label: "Info leakage" },
};

export function sortByTier(items: ActionItem[] | undefined): ActionItem[] {
  return [...(items ?? [])].sort(
    (a, b) => (TIER_ORDER[a.tier] ?? 9) - (TIER_ORDER[b.tier] ?? 9),
  );
}

export function groupByDimension(
  items: ActionItem[] | undefined,
): Record<Dimension, ActionItem[]> {
  const groups: Record<Dimension, ActionItem[]> = { verifier: [], oracle: [], info_leakage: [] };
  for (const item of items ?? []) (groups[item.dimension] ??= []).push(item);
  (Object.keys(groups) as Dimension[]).forEach((k) => (groups[k] = sortByTier(groups[k])));
  return groups;
}
