import type { AgentCapabilities } from "@/lib/types";

/** behavior_discovery is absent on purpose: DISCOVERY_CAP bounds its evidence
 *  at two observations, so its magnitude reflects the prompt's budget rather
 *  than how the agents behaved. It stays in the prose below the chart. */
export const CHART_CATEGORIES = [
  "planning",
  "testing_verification",
  "debugging",
  "scope_adherence",
  "coherence",
  "environment_tooling",
] as const;

/** Distinct cited trials below which a bar is drawn faded. */
export const THIN_N = 5;

export type CategoryDelta = {
  category: string;
  n: number;
  ratio: number | null;
  delta: number | null;
};

export function pooledDeltas(comparison: AgentCapabilities): CategoryDelta[] {
  const successes = comparison.cohort_success.length;
  const failures = comparison.cohort_failure.length;
  // A one-sided cohort has no baseline to be measured against. Every citable
  // trial sits on the same side, so the ratio equals the baseline exactly and
  // all six categories read a confident +0.00 -- "no divergence" drawn where
  // there was no other side to diverge from. The delta is undefined here, not
  // zero, and the caller drops a chart whose every delta is null.
  const comparable = successes > 0 && failures > 0;
  const baseline = comparable ? successes / (successes + failures) : 0;
  const byCategory = new Map(comparison.categories.map((c) => [c.category, c]));

  return CHART_CATEGORIES.map((category) => {
    const cat = byCategory.get(category);
    // Distinct trials, not citations: one observation may cite the same run
    // several times, and that must not outweigh a pattern across runs.
    const success = new Set<string>();
    const failure = new Set<string>();
    for (const obs of cat?.successful ?? [])
      for (const ev of obs.evidence ?? []) success.add(ev.trial_id);
    for (const obs of cat?.failing ?? [])
      for (const ev of obs.evidence ?? []) failure.add(ev.trial_id);

    const n = success.size + failure.size;
    // No evidence is not the same claim as no advantage; a zero would plot as
    // the latter, so it stays null and the bar renders as absent.
    const ratio = n ? success.size / n : null;
    return {
      category,
      n,
      ratio,
      delta: ratio === null || !comparable ? null : ratio - baseline,
    };
  });
}
