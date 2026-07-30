"use client";

import { formatCostUsd, hasDisplayableCostUsd } from "@/lib/format";
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

// Red / yellow / brown, worst to least. Solid fills, like the status badges.
const TIER_CLASS: Record<string, string> = {
  must_fix: "bg-[color:var(--paper-fail)] text-white",
  should_fix: "bg-[color:var(--paper-partial)] text-slate-950",
  optional: "bg-[color:var(--paper-minor)] text-white",
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
  findingCount: number
): PreTrialAuditState {
  if (!status) return "unaudited";
  const normalized = status.toLowerCase();
  if (normalized === "running" || normalized === "queued") return "running";
  if (normalized === "success") return findingCount > 0 ? "findings" : "clean";
  // failed, cancelled, or anything the backend grows later: never claim clean.
  return "failed";
}

/**
 * The findings themselves, worst tier first, as one card with hairline rules.
 *
 * Shared so the trial sidebar renders findings identically to the task page
 * rather than re-deriving the layout and drifting from it.
 */
export function PreTrialFindingList({ items }: { items: PreTrialFinding[] }) {
  const sorted = [...items].sort(
    (a, b) => (TIER_ORDER[a.tier ?? ""] ?? 3) - (TIER_ORDER[b.tier ?? ""] ?? 3)
  );
  if (!sorted.length) return null;

  return (
    <div className="divide-y divide-[color:var(--paper-line)] overflow-hidden rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)]">
      {sorted.map((finding, index) => {
        const tier = finding.tier ?? "optional";
        const where = location(finding);
        return (
          <div
            key={finding.id ?? `${finding.title}-${index}`}
            className="space-y-2 px-4 py-3"
          >
            <div className="flex flex-wrap items-center gap-2">
              <span
                className={`rounded-[4px] px-1.5 py-0.5 font-mono text-[9.5px] tracking-[0.06em] ${
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

/**
 * Pre-trial source-audit findings for the selected task version.
 *
 * Renders nothing until a version has actually been audited.
 */
// Hardcoded feature flag: shows the run/re-run pre-trial audit button.
// Testing-only for now — flip to false to hide it without deleting code.
const ENABLE_PRETRIAL_RERUN_BUTTON = true;

export function TaskPreTrialCard({
  findings,
  status,
  error,
  costUsd,
  onRerun,
  rerunning,
  queueError,
  isCurrentVersion = true,
  taskQaBusy = false,
}: {
  findings?: PreTrialFinding[];
  status?: string | null;
  error?: string | null;
  costUsd?: number | null;
  /** Queue a fresh pre-trial audit of the task's current version. */
  onRerun?: () => void;
  rerunning?: boolean;
  /** Error from the last attempt to queue an audit, shown on this card. */
  queueError?: string | null;
  /** Audits run on the current version only; false disables the button. */
  isCurrentVersion?: boolean;
  /** Task-level QA is queued or running; the backend rejects audits then. */
  taskQaBusy?: boolean;
}) {
  const items = [...(findings ?? [])].sort(
    (a, b) => (TIER_ORDER[a.tier ?? ""] ?? 3) - (TIER_ORDER[b.tier ?? ""] ?? 3)
  );
  const state = preTrialAuditState(status, items.length);

  // Always render the button; disable it with the reason in the tooltip. A
  // hidden button with no explanation is impossible to debug from the UI.
  const blockedReason =
    state === "running"
      ? "An audit is already running"
      : taskQaBusy
        ? "Task-level QA is running; wait for it to finish"
        : !isCurrentVersion
          ? "Audits run on the current version only. Select the current version to run one."
          : null;
  const rerunButton =
    ENABLE_PRETRIAL_RERUN_BUTTON && onRerun ? (
      <button
        type="button"
        disabled={rerunning || blockedReason !== null}
        onClick={onRerun}
        className="rounded border border-[color:var(--paper-line)] px-1.5 py-0.5 font-mono text-[10px] text-[color:var(--paper-ink-3)] hover:text-[color:var(--paper-ink)] disabled:cursor-not-allowed disabled:opacity-50"
        title={
          blockedReason ??
          "Audit the task's current version again with the latest prompt"
        }
      >
        {rerunning
          ? "Queuing…"
          : state === "unaudited"
            ? "Run audit"
            : "Re-run audit"}
      </button>
    ) : null;

  const queueErrorLine = queueError ? (
    <p className="text-[11px] text-[color:var(--paper-fail)]">{queueError}</p>
  ) : null;

  if (state === "unaudited") {
    if (!rerunButton) return null;
    return (
      <div className="space-y-3">
        <div className="flex items-baseline justify-between">
          <h2 className="font-mono text-[12px] font-semibold tracking-[0.06em] text-[color:var(--paper-ink-2)] uppercase">
            Pre-trial audit
          </h2>
          {rerunButton}
        </div>
        {queueErrorLine}
        <p className="text-[12px] text-[color:var(--paper-ink-3)]">
          The task source has not been audited.
        </p>
      </div>
    );
  }

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
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="font-mono text-[12px] font-semibold tracking-[0.06em] text-[color:var(--paper-ink-2)] uppercase">
          Pre-trial audit
        </h2>
        <div className="flex items-baseline gap-2">
          {rerunButton}
          <span
            className={`font-mono text-[10.5px] ${
              state === "failed"
                ? "text-[color:var(--paper-fail)]"
                : "text-[color:var(--paper-ink-3)]"
            }`}
          >
            {summary}
            {hasDisplayableCostUsd(costUsd)
              ? ` · ${formatCostUsd(costUsd)}`
              : ""}
          </span>
        </div>
      </div>

      {queueErrorLine}

      {state === "failed" && error ? (
        <div className="rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-4 py-3 font-mono text-[11px] break-all text-[color:var(--paper-ink-3)]">
          {error}
        </div>
      ) : null}

      <PreTrialFindingList items={items} />
    </div>
  );
}
