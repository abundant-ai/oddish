import { formatCostUsd } from "@/lib/format";

// Callers decide whether an amount is known: null means unavailable, while
// an explicit zero can describe an experiment that only gathered other runs.
export function CostValue({
  cost,
  hasEstimated,
  hasNative,
  title,
}: {
  cost: number | null;
  hasEstimated: boolean;
  hasNative: boolean;
  title: string;
}) {
  const provenance =
    hasEstimated && hasNative
      ? ". Mixed native + estimated values; ~ marks estimates."
      : hasEstimated
        ? ". Estimated from token counts × static model pricing."
        : ". Reported by the agent runtime.";
  return (
    <span
      className="inline-flex items-baseline gap-1 tabular-nums"
      title={
        title + (cost !== null && (hasEstimated || hasNative) ? provenance : "")
      }
    >
      {cost === null ? (
        <span className="text-[color:var(--paper-ink-3)]">—</span>
      ) : (
        <>
          {hasEstimated && !hasNative && (
            <span className="text-[color:var(--paper-ink-3)]">~</span>
          )}
          {formatCostUsd(cost)}
          {hasEstimated && hasNative && (
            <span className="text-[color:var(--paper-ink-3)]">*</span>
          )}
        </>
      )}
    </span>
  );
}
