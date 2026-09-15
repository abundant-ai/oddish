import { formatCostUsd, hasDisplayableCostUsd } from "@/lib/format";

// Sidecar spend rendered next to the agent-cost figure it annotates.
// Deliberately NOT summed into that figure: the headline number keeps its
// existing meaning.
//
// `tile` sits beside a 26px display figure; `row` beside body text.
const SIZES = {
  tile: "text-[13px]",
  row: "text-[11px]",
} as const;

function CostSuffix({
  label,
  costUsd,
  size = "row",
  title,
}: {
  label: string;
  costUsd: number | null | undefined;
  size?: keyof typeof SIZES;
  title?: string;
}) {
  // Nothing, not "+$0.00 …" -- most trials have no sidecar spend, and
  // sub-cent amounts round away. formatCostUsd never returns "", so the
  // guard has to be here.
  if (!hasDisplayableCostUsd(costUsd)) return null;

  return (
    <span
      className={`font-mono font-normal text-[color:var(--paper-ink-3)] ${SIZES[size]}`}
      title={title}
    >
      +{formatCostUsd(costUsd)} {label}
    </span>
  );
}

export function QaCostSuffix({
  costUsd,
  size = "row",
  title,
}: {
  costUsd: number | null | undefined;
  size?: keyof typeof SIZES;
  title?: string;
}) {
  return <CostSuffix label="QA" costUsd={costUsd} size={size} title={title} />;
}

export function VerifierCostSuffix({
  costUsd,
  size = "row",
  title = "CUA/verifier LLM spend for this trial. Not included in the cost figure.",
}: {
  costUsd: number | null | undefined;
  size?: keyof typeof SIZES;
  title?: string;
}) {
  return (
    <CostSuffix label="Verifier" costUsd={costUsd} size={size} title={title} />
  );
}
