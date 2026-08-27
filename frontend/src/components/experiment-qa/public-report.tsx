import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { ExperimentQaGlancePanel } from "./glance-panel";
import { ExperimentQaResultChip, ExperimentQaStatusChip } from "./result-chip";
import {
  EXPERIMENT_QA_SOURCE_LABEL,
  experimentQaSignal,
} from "@/lib/experiment-qa";
import type { PublicExperimentQaReport } from "@/lib/types";

function formatPublishedAt(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("en", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "UTC",
  }).format(date);
}

function locationLabel({
  file,
  line_start: start,
  line_end: end,
}: {
  file: string | null;
  line_start: number | null;
  line_end: number | null;
}): string | null {
  if (!file) return null;
  if (!start) return file;
  return `${file}:${start}${end && end !== start ? `-${end}` : ""}`;
}

export function ExperimentQaPublicReport({
  report,
  experimentHref,
  preview = false,
}: {
  report: PublicExperimentQaReport;
  experimentHref: string;
  preview?: boolean;
}) {
  const tasks = report.tasks.filter((task) => task.items.length > 0);

  return (
    <div className="mx-auto w-full max-w-[980px] space-y-4">
      <header className="space-y-1">
        <Link
          href={experimentHref}
          className="text-paper-ink-3 hover:text-paper-ink inline-flex items-center gap-1 py-1 font-mono text-[11px] transition-colors"
        >
          <ArrowLeft className="size-3" aria-hidden="true" />
          {preview ? "Back to QA editor" : "Back to experiment"}
        </Link>
        <p className="text-paper-ink-3 font-mono text-[11px]">
          {report.experiment.name}
        </p>
        <div className="flex flex-wrap items-center gap-2.5">
          <h1 className="text-paper-ink font-mono text-[26px] leading-[1.25] font-semibold tracking-[-0.02em]">
            {report.title || "QA"}
          </h1>
          <ExperimentQaStatusChip status={preview ? "preview" : "published"} />
        </div>
        <p className="text-paper-ink-3 font-mono text-[11px]">
          {preview ? "Preview from the current draft" : "Published"}{" "}
          {formatPublishedAt(report.published_at)} UTC
        </p>
      </header>

      {report.summary || report.conclusion || report.customer_note ? (
        <section className="border-paper-line bg-paper-surface rounded-[10px] border p-4">
          {report.summary ? (
            <div>
              <h2 className="text-paper-ink-3 font-mono text-[10px] font-semibold tracking-[0.09em] uppercase">
                What we tested
              </h2>
              <p className="text-paper-ink mt-1 text-[13.5px] leading-relaxed">
                {report.summary}
              </p>
            </div>
          ) : null}
          {report.conclusion ? (
            <div
              className={
                report.summary ? "border-paper-line-2 mt-3 border-t pt-3" : ""
              }
            >
              <h2 className="text-paper-ink-3 font-mono text-[10px] font-semibold tracking-[0.09em] uppercase">
                Conclusion
              </h2>
              <p className="text-paper-ink mt-1 text-[13.5px] leading-relaxed">
                {report.conclusion}
              </p>
            </div>
          ) : null}
          {report.customer_note ? (
            <div
              className={
                report.summary || report.conclusion
                  ? "border-paper-line-2 mt-3 border-t pt-3"
                  : ""
              }
            >
              <h2 className="text-paper-ink-3 font-mono text-[10px] font-semibold tracking-[0.09em] uppercase">
                Note
              </h2>
              <p className="text-paper-ink-2 mt-1 text-[12.5px] leading-relaxed">
                {report.customer_note}
              </p>
            </div>
          ) : null}
        </section>
      ) : null}

      <ExperimentQaGlancePanel tasks={tasks} />

      {tasks.length === 0 ? (
        preview ? (
          <div className="border-paper-line bg-paper-surface rounded-[10px] border p-6 text-center">
            <p className="text-paper-ink-2 text-[13px]">
              Select the QA checks you want to share, then open the preview
              again.
            </p>
          </div>
        ) : null
      ) : (
        <div className="border-paper-line bg-paper-surface overflow-hidden rounded-[10px] border">
          {tasks.map((task, taskIndex) => (
            <section
              key={`${taskIndex}-${task.name}`}
              aria-label={task.name}
              className={taskIndex > 0 ? "border-paper-line border-t" : ""}
            >
              <div className="border-paper-line-2 bg-paper-surface-2 border-b px-4 py-3">
                <h2 className="text-paper-ink font-mono text-[13px] font-semibold">
                  {task.name}
                </h2>
                {task.summary ? (
                  <p className="text-paper-ink-2 mt-1 text-[12.5px] leading-relaxed">
                    {task.summary}
                  </p>
                ) : null}
              </div>
              <div className="grid gap-2.5 p-3 sm:p-4">
                {task.items.map((item, itemIndex) => {
                  const signal = experimentQaSignal(item);
                  const where = locationLabel(item);
                  return (
                    <article
                      key={`${itemIndex}-${item.source_type}-${item.title ?? "check"}`}
                      className="border-paper-line bg-paper-surface rounded-lg border p-3 shadow-xs"
                    >
                      <div className="flex flex-wrap items-start gap-2">
                        <ExperimentQaResultChip signal={signal} />
                        <span className="text-paper-ink-3 ml-auto font-mono text-[10px]">
                          {EXPERIMENT_QA_SOURCE_LABEL[item.source_type]}
                        </span>
                      </div>
                      <h3 className="text-paper-ink mt-2 text-[13px] leading-snug font-semibold">
                        {item.title || "QA finding"}
                      </h3>
                      {item.summary ? (
                        <p className="text-paper-ink-2 mt-1.5 text-[12.5px] leading-relaxed whitespace-pre-wrap">
                          {item.summary}
                        </p>
                      ) : null}
                      {item.recommendation ? (
                        <div className="mt-2 flex items-baseline gap-2">
                          <span className="text-paper-ink-3 shrink-0 font-mono text-[10px] tracking-widest">
                            FIX
                          </span>
                          <p className="text-paper-ink-2 text-[12px] leading-relaxed">
                            {item.recommendation}
                          </p>
                        </div>
                      ) : null}
                      {item.customer_note ? (
                        <p className="text-paper-ink-3 mt-2 text-[12px] leading-relaxed italic">
                          {item.customer_note}
                        </p>
                      ) : null}
                      {where ? (
                        <p className="text-paper-ink-3 mt-2 font-mono text-[10.5px] break-all">
                          {where}
                        </p>
                      ) : null}
                      {item.evidence ? (
                        <details className="group border-paper-line mt-2.5 border-l-2 pl-3">
                          <summary className="text-paper-ink-3 hover:text-paper-ink cursor-pointer list-none font-mono text-[11px] transition-colors select-none">
                            <span
                              aria-hidden="true"
                              className="mr-1.5 inline-block text-[9px] transition-transform group-open:rotate-90"
                            >
                              &#9654;
                            </span>
                            Evidence
                          </summary>
                          <p className="bg-paper-surface-2 text-paper-ink-2 mt-2 rounded-r-md p-2 font-mono text-[11px] leading-relaxed whitespace-pre-wrap">
                            {item.evidence}
                          </p>
                        </details>
                      ) : null}
                    </article>
                  );
                })}
              </div>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
