import { formatCostUsd, hasDisplayableCostUsd } from "@/lib/format";

// QA spend rendered as a muted sidecar to the agent-cost figure it annotates.
// Deliberately NOT summed into that figure: the headline number keeps its
// existing meaning, and QA is secondary information.
//
// `tile` sits beside a 26px display figure; `row` beside body text.
const SIZES = {
  tile: "text-[13px]",
  row: "text-[11px]",
} as const;

export function QaCostSuffix({
  costUsd,
  size = "row",
  title,
}: {
  costUsd: number | null | undefined;
  size?: keyof typeof SIZES;
  title?: string;
}) {
  // Nothing, not "+$0.00 QA" -- most trials have no QA, and sub-cent QA rounds
  // away. formatCostUsd never returns "", so the guard has to be here.
  if (!hasDisplayableCostUsd(costUsd)) return null;

  return (
    <span
      className={`font-mono font-normal text-[color:var(--paper-ink-3)] ${SIZES[size]}`}
      title={title}
    >
      +{formatCostUsd(costUsd)} QA
    </span>
  );
}
