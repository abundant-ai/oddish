import type { CostSeries } from "@/lib/types";

/** Stack key used by `series_by_type` for CUA / verifier LLM spend. */
export const VERIFIER_TYPE_KEY = "verifier";

/** Extra segment on Agent / Model / User charts so CUA stays visible. */
export const VERIFIER_OVERLAY_KEY = "verifier";
export const VERIFIER_OVERLAY_LABEL = "Verifier (CUA)";

function verifierAmount(costs: Record<string, number> | undefined): number {
  const amount = costs?.[VERIFIER_TYPE_KEY] ?? 0;
  return Number.isFinite(amount) && amount > 0 ? amount : 0;
}

/**
 * Copy CUA verifier dollars from the cost-type series onto another stack
 * (agent, model, or user). Those series are solver inference only, so without
 * this overlay the default chart hides verifier spend.
 *
 * No-op when the type series has no verifier dollars, or when the base series
 * is already the type stack (it already has the key).
 */
export function overlayVerifierSeries(
  base: CostSeries,
  typeSeries: CostSeries | undefined
): CostSeries {
  if (!typeSeries || base.dimension === "type") return base;

  const extraByBucket = new Map<string, number>();
  for (const bucket of typeSeries.buckets) {
    const amount = verifierAmount(bucket.costs);
    if (amount > 0) extraByBucket.set(bucket.bucket_start, amount);
  }
  if (extraByBucket.size === 0) return base;

  const starts = new Set<string>([
    ...base.buckets.map((bucket) => bucket.bucket_start),
    ...extraByBucket.keys(),
  ]);
  const sorted = [...starts].sort();
  const baseByStart = new Map(
    base.buckets.map((bucket) => [bucket.bucket_start, bucket])
  );

  const keys = base.keys.some((key) => key.key === VERIFIER_OVERLAY_KEY)
    ? base.keys
    : [...base.keys, { key: VERIFIER_OVERLAY_KEY, label: VERIFIER_OVERLAY_LABEL }];

  const buckets = sorted.map((start) => {
    const existing = baseByStart.get(start);
    const verifier = extraByBucket.get(start) ?? 0;
    const costs = { ...(existing?.costs ?? {}), [VERIFIER_OVERLAY_KEY]: verifier };
    const cost_usd = Object.values(costs).reduce((sum, value) => sum + value, 0);
    return {
      bucket_start: start,
      cost_usd,
      trial_count: existing?.trial_count ?? 0,
      costs,
    };
  });

  return { ...base, keys, buckets };
}
