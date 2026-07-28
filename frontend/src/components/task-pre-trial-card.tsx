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

export type PreTrialAuditState =
  | "unaudited"
  | "running"
  | "failed"
  | "clean"
  | "findings";

/**
 * What the card should say for a version's audit.
 *
 * Empty findings mean three different things depending on status, and
 * conflating them reports in-flight and failed audits as clean passes:
 * `sync_pre_trial_to_task_version` *clears* `pre_trial` when it records a
 * failure, and the claim sets `running` before the agent does any work. Only
 * `success` with no items is genuinely "we looked and found nothing".
 */
export function preTrialAuditState(
  status: string | null | undefined,
  findingCount: number,
): PreTrialAuditState {
  if (!status) return "unaudited";
  const normalized = status.toLowerCase();
  if (normalized === "running" || normalized === "queued") return "running";
  if (normalized === "success") return findingCount > 0 ? "findings" : "clean";
  // failed, cancelled, or anything the backend grows later: never claim clean.
  return "failed";
}

/**
 * Pre-trial source-audit findings for the selected task version.
 *
 * Renders nothing until a version has actually been audited.
 */
export function TaskPreTrialCard({
  findings,
  status,
  error,
}: {
  findings?: PreTrialFinding[];
  status?: string | null;
  error?: string | null;
}) {
  const items = [...(findings ?? [])].sort(
    (a, b) =>
      (TIER_ORDER[a.tier ?? ""] ?? 3) - (TIER_ORDER[b.tier ?? ""] ?? 3),
  );
  const state = preTrialAuditState(status, items.length);

  if (state === "unaudited") return null;

  const summary =
    state === "running"
      ? "running…"
      : state === "failed"
        ? "audit failed"
        : state === "clean"
          ? "no defects found"
          : `${items.length} finding${items.length === 1 ? "" : "s"}`;

  return (
    <div className="space-y-3">
      <div className="flex items-baseline justify-between">
        <h2 className="font-mono text-[12px] font-semibold tracking-[0.06em] text-[color:var(--paper-ink-2)] uppercase">
          Pre-trial audit
        </h2>
        <span
          className={`font-mono text-[10.5px] ${
            state === "failed"
              ? "text-[color:var(--paper-fail)]"
              : "text-[color:var(--paper-ink-3)]"
          }`}
        >
          {summary}
        </span>
      </div>

      {state === "failed" && error ? (
        <div className="rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-4 py-3 font-mono text-[11px] break-all text-[color:var(--paper-ink-3)]">
          {error}
        </div>
      ) : null}

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
