import { formatCostUsd } from "@/lib/format";

// QA spend rendered as a muted annotation on the agent-cost figure it
// accompanies. Deliberately NOT summed into that figure: the headline number
// keeps its existing meaning, and QA is secondary information.
//
// `sub` sits on its own line beneath a display figure; `row` sits inline
// beside body text. Only the inline variant carries the leading "+": on its
// own line the amount can't be misread as part of the figure above it, so the
// sign is noise there.
const SIZES = {
  sub: "text-[10px]",
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
  // Nothing, not "+$0.00 QA" -- most trials have no QA and the suffix would be
  // pure noise. formatCostUsd(0) returns "$0.00", so the guard has to be here.
  if (costUsd == null || !Number.isFinite(costUsd) || costUsd <= 0) return null;

  return (
    <span
      className={`font-mono font-normal text-[color:var(--paper-ink-3)] ${SIZES[size]}`}
      title={title}
    >
      {size === "row" ? "+" : ""}
      {formatCostUsd(costUsd)} QA
    </span>
  );
}
