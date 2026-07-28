"use client";

import type { PreTrialFinding } from "@/lib/types";

// Worst first: a must_fix can misgrade a trial, the rest are hygiene.
const TIER_ORDER: Record<string, number> = {
  must_fix: 0,
  should_fix: 1,
  optional: 2,
};

const TIER_LABEL: Record<string, string> = {
  must_fix: "MUST FIX",
  should_fix: "SHOULD FIX",
  optional: "OPTIONAL",
};

const TIER_CLASS: Record<string, string> = {
  must_fix: "border-[color:var(--paper-fail)] text-[color:var(--paper-fail)]",
  should_fix: "border-[color:var(--paper-partial)] text-[color:var(--paper-partial)]",
  optional: "border-[color:var(--paper-line)] text-[color:var(--paper-ink-3)]",
};

function location(finding: PreTrialFinding): string | null {
  if (!finding.file) return null;
  const { line_start: start, line_end: end } = finding;
  if (!start) return finding.file;
  return `${finding.file}:${start}${end && end !== start ? `-${end}` : ""}`;
}

/**
 * Pre-trial source-audit findings for the selected task version.
 *
 * Renders nothing until a version has actually been audited: an audit that
 * found no defects still reports `pre_trial_status`, so a clean result is
 * distinguishable from one that never ran and is worth saying out loud.
 */
export function TaskPreTrialCard({
  findings,
  status,
}: {
  findings?: PreTrialFinding[];
  status?: string | null;
}) {
  if (!status) return null;

  const items = [...(findings ?? [])].sort(
    (a, b) =>
      (TIER_ORDER[a.tier ?? ""] ?? 3) - (TIER_ORDER[b.tier ?? ""] ?? 3),
  );

  return (
    <div className="space-y-3">
      <div className="flex items-baseline justify-between">
        <h2 className="font-mono text-[12px] font-semibold tracking-[0.06em] text-[color:var(--paper-ink-2)] uppercase">
          Pre-trial audit
        </h2>
        <span className="font-mono text-[10.5px] text-[color:var(--paper-ink-3)]">
          {items.length === 0
            ? "no defects found"
            : `${items.length} finding${items.length === 1 ? "" : "s"}`}
        </span>
      </div>

      {items.map((finding, index) => {
        const tier = finding.tier ?? "optional";
        const where = location(finding);
        return (
          <div
            key={finding.id ?? `${finding.title}-${index}`}
            className="rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-4 py-3 space-y-2"
          >
            <div className="flex flex-wrap items-center gap-2">
              <span
                className={`rounded-[4px] border px-1.5 py-0.5 font-mono text-[9.5px] tracking-[0.06em] ${
                  TIER_CLASS[tier] ?? TIER_CLASS.optional
                }`}
              >
                {TIER_LABEL[tier] ?? tier.toUpperCase()}
              </span>
              {finding.dimension ? (
                <span className="font-mono text-[10px] text-[color:var(--paper-ink-3)]">
                  {finding.dimension}
                  {finding.problem_type ? ` / ${finding.problem_type}` : ""}
                </span>
              ) : null}
              {finding.exploited ? (
                <span className="rounded-[4px] border border-[color:var(--paper-fail)] px-1.5 py-0.5 font-mono text-[9.5px] tracking-[0.06em] text-[color:var(--paper-fail)]">
                  EXPLOITED
                </span>
              ) : null}
            </div>

            <p className="text-[13px] font-medium text-[color:var(--paper-ink)]">
              {finding.title}
            </p>

            {where ? (
              <p className="font-mono text-[10.5px] break-all text-[color:var(--paper-ink-3)]">
                {where}
              </p>
            ) : null}

            {finding.detail ? (
              <p className="text-[12px] whitespace-pre-wrap text-[color:var(--paper-ink-2)]">
                {finding.detail}
              </p>
            ) : null}

            {finding.recommendation ? (
              <p className="text-[12px] text-[color:var(--paper-ink-2)]">
                <span className="font-mono text-[10.5px] tracking-[0.06em] text-[color:var(--paper-ink-3)] uppercase">
                  Fix{" "}
                </span>
                {finding.recommendation}
              </p>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
