import type { ExperimentQaItemContent } from "@/lib/types";
import {
  EXPERIMENT_QA_SOURCE_LABEL,
  experimentQaSignal,
  type ExperimentQaSignal,
} from "@/lib/experiment-qa";

const SIGNALS: ExperimentQaSignal[] = [
  "valid",
  "needs_work",
  "false_positive",
  "harness",
  "running",
  "unknown",
];

const SIGNAL_LABEL: Record<ExperimentQaSignal, string> = {
  valid: "Valid",
  needs_work: "Needs work",
  false_positive: "False positive",
  harness: "Harness",
  running: "Running",
  unknown: "Review",
};

const SIGNAL_COLOR: Record<ExperimentQaSignal, string> = {
  valid: "var(--paper-pass)",
  needs_work: "var(--paper-partial)",
  false_positive: "var(--paper-fail)",
  harness: "var(--paper-minor)",
  running: "var(--paper-running)",
  unknown: "var(--paper-ink-4)",
};

type GlanceItem = Pick<
  ExperimentQaItemContent,
  "outcome" | "tier" | "source_type"
>;

type GlanceTask = { items: GlanceItem[] };

function emptySignalCounts() {
  return SIGNALS.reduce<Record<ExperimentQaSignal, number>>(
    (counts, signal) => ({ ...counts, [signal]: 0 }),
    {} as Record<ExperimentQaSignal, number>
  );
}

function StackedBar({ items }: { items: GlanceItem[] }) {
  const counts = emptySignalCounts();
  for (const item of items) counts[experimentQaSignal(item)] += 1;
  const total = items.length;

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-3">
        <span className="text-paper-ink-3 font-mono text-[10px] font-semibold tracking-[0.09em] uppercase">
          QA checks by result
        </span>
        <span className="text-paper-ink-3 font-mono text-[11px]">
          {total} total
        </span>
      </div>
      <div
        className="border-paper-line bg-paper-surface flex h-[18px] w-full overflow-hidden rounded border"
        aria-label={`${total} QA checks`}
      >
        {SIGNALS.map((signal) =>
          counts[signal] > 0 ? (
            <span
              key={signal}
              title={`${SIGNAL_LABEL[signal]}: ${counts[signal]}`}
              style={{
                background: SIGNAL_COLOR[signal],
                flexGrow: counts[signal],
              }}
            />
          ) : null
        )}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1.5">
        {SIGNALS.filter((signal) => counts[signal] > 0).map((signal) => (
          <span
            key={signal}
            className="text-paper-ink-2 inline-flex items-center gap-1.5 font-mono text-[11px]"
          >
            <span
              className="size-2.5 rounded-sm"
              style={{ background: SIGNAL_COLOR[signal] }}
              aria-hidden="true"
            />
            {SIGNAL_LABEL[signal]} {counts[signal]}
          </span>
        ))}
      </div>
    </div>
  );
}

export function ExperimentQaGlancePanel({ tasks }: { tasks: GlanceTask[] }) {
  const items = tasks.flatMap((task) => task.items);
  const sources = ["pre_trial", "verdict", "trial_analysis"] as const;

  return (
    <section className="border-paper-line bg-paper-surface grid overflow-hidden rounded-[10px] border md:grid-cols-[1.1fr_1fr]">
      <div className="md:border-paper-line-2 p-3.5 md:border-r">
        <StackedBar items={items} />
      </div>
      <div className="border-paper-line-2 border-t p-3.5 md:border-t-0">
        <div className="mb-2 flex items-center justify-between gap-3">
          <span className="text-paper-ink-3 font-mono text-[10px] font-semibold tracking-[0.09em] uppercase">
            Checks by source
          </span>
          <span className="text-paper-ink-3 font-mono text-[11px]">
            {tasks.length} tasks
          </span>
        </div>
        <div className="grid gap-1.5">
          {sources.map((source) => {
            const count = items.filter(
              (item) => item.source_type === source
            ).length;
            return (
              <div
                key={source}
                className="border-paper-line-2 flex items-center justify-between gap-4 border-b py-1.5 last:border-0"
              >
                <span className="text-paper-ink-2 text-[12px]">
                  {EXPERIMENT_QA_SOURCE_LABEL[source]}
                </span>
                <span className="text-paper-ink font-mono text-[11px] font-semibold">
                  {count}
                </span>
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}
