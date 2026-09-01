"use client";

import { useEffect, useMemo, useState } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { TrialDetailPanel } from "@/components/trial-detail-panel";
import { formatCostUsd, hasDisplayableCostUsd } from "@/lib/format";
import { isActiveTrialStatus } from "@/lib/job-status";
import type {
  QAEvalExperimentResponse,
  QAEvalExperimentTrial,
  Trial,
} from "@/lib/types";
import { cn, urlWithSearch } from "@/lib/utils";
import {
  AlertCircle,
  CheckCircle2,
  Clock3,
  Loader2,
  XCircle,
} from "lucide-react";

interface QAEvalExperimentViewProps {
  experiment: QAEvalExperimentResponse;
  headerLeft: React.ReactNode;
  headerDescription?: React.ReactNode;
  inlineAlert?: React.ReactNode;
}

const TERMINAL_TRIAL_STATUSES = new Set(["success", "failed", "skipped"]);

function singleValue(values: string[]): string {
  if (values.length === 0) return "missing";
  if (values.length === 1) return values[0];
  return `mixed (${values.join(", ")})`;
}

function outputState(entry: QAEvalExperimentTrial): {
  label: string;
  tone: string;
  Icon: typeof CheckCircle2;
} {
  if (entry.stored_payload_error) {
    return {
      label: "stored metadata invalid",
      tone: "text-red-600 dark:text-red-400",
      Icon: XCircle,
    };
  }
  if (entry.trial.analysis_status === "success" && entry.trial.analysis) {
    return {
      label: "valid QA JSON",
      tone: "text-emerald-600 dark:text-emerald-400",
      Icon: CheckCircle2,
    };
  }
  if (entry.trial.analysis_status === "failed") {
    return {
      label: "QA JSON rejected",
      tone: "text-red-600 dark:text-red-400",
      Icon: XCircle,
    };
  }
  if (entry.trial.status === "failed") {
    return {
      label: "QA run failed",
      tone: "text-red-600 dark:text-red-400",
      Icon: AlertCircle,
    };
  }
  if (
    isActiveTrialStatus(entry.trial.status) ||
    ["pending", "queued", "running"].includes(entry.trial.analysis_status ?? "")
  ) {
    return {
      label: entry.trial.status,
      tone: "text-blue-600 dark:text-blue-400",
      Icon: Loader2,
    };
  }
  return {
    label: "waiting for import",
    tone: "text-amber-600 dark:text-amber-400",
    Icon: Clock3,
  };
}

function updateTrialUrl(trialId: string | null) {
  const params = new URLSearchParams(window.location.search);
  if (trialId) params.set("trial", trialId);
  else {
    params.delete("trial");
    params.delete("tab");
    params.delete("file");
    params.delete("lines");
  }
  window.history.pushState(
    window.history.state,
    "",
    urlWithSearch(params.toString())
  );
}

export function QAEvalExperimentView({
  experiment,
  headerLeft,
  headerDescription,
  inlineAlert,
}: QAEvalExperimentViewProps) {
  const orderedTrials = useMemo(
    () => experiment.trials.map((entry) => entry.trial),
    [experiment.trials]
  );
  const [selectedTrialId, setSelectedTrialId] = useState<string | null>(() => {
    if (typeof window === "undefined") return null;
    const requested = new URLSearchParams(window.location.search).get("trial");
    return orderedTrials.some((trial) => trial.id === requested)
      ? requested
      : null;
  });
  const selectedIndex = orderedTrials.findIndex(
    (trial) => trial.id === selectedTrialId
  );
  const selectedTrial =
    selectedIndex >= 0 ? orderedTrials[selectedIndex] : null;

  useEffect(() => {
    const restoreSelection = () => {
      const requested = new URLSearchParams(window.location.search).get(
        "trial"
      );
      setSelectedTrialId(
        orderedTrials.some((trial) => trial.id === requested) ? requested : null
      );
    };
    window.addEventListener("popstate", restoreSelection);
    return () => window.removeEventListener("popstate", restoreSelection);
  }, [orderedTrials]);

  const openTrial = (trial: Trial) => {
    setSelectedTrialId(trial.id);
    updateTrialUrl(trial.id);
  };
  const closeTrial = () => {
    setSelectedTrialId(null);
    updateTrialUrl(null);
  };

  const terminalCount = experiment.trials.filter((entry) =>
    TERMINAL_TRIAL_STATUSES.has(entry.trial.status)
  ).length;
  const validOutputCount = experiment.trials.filter(
    (entry) =>
      !entry.stored_payload_error &&
      entry.trial.analysis_status === "success" &&
      Boolean(entry.trial.analysis?.classification)
  ).length;
  const rejectedOutputCount = experiment.trials.filter(
    (entry) =>
      Boolean(entry.stored_payload_error) ||
      entry.trial.analysis_status === "failed" ||
      entry.trial.status === "failed"
  ).length;
  const classificationCounts = experiment.trials.reduce<Record<string, number>>(
    (counts, entry) => {
      const classification = entry.trial.analysis?.classification;
      if (classification)
        counts[classification] = (counts[classification] ?? 0) + 1;
      return counts;
    },
    {}
  );
  const totalCost = experiment.trials.reduce(
    (sum, entry) => sum + (entry.trial.cost_usd ?? 0),
    0
  );
  const hasMixedMetadata =
    experiment.prompt_names.length !== 1 ||
    experiment.prompt_sha256s.length !== 1 ||
    experiment.models.length !== 1;

  return (
    <>
      <div className="space-y-4">
        <div className="space-y-2">
          <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
            <div className="flex min-w-0 flex-1 flex-col gap-1">
              {headerLeft}
              <div className="flex flex-wrap items-center gap-x-2 font-mono text-[11px] text-[color:var(--paper-ink-3)]">
                <span>private QA prompt replay</span>
                <span>·</span>
                <span>{experiment.experiment_id}</span>
                <span>·</span>
                <span>{experiment.trials.length} source solver trials</span>
              </div>
            </div>
          </div>
          {headerDescription}
        </div>

        <div className="grid overflow-hidden rounded-lg border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] sm:grid-cols-4">
          {[
            [
              "QA runs finished",
              `${terminalCount}/${experiment.trials.length}`,
            ],
            ["Valid QA JSON", String(validOutputCount)],
            ["Rejected or failed", String(rejectedOutputCount)],
            ["QA model cost", formatCostUsd(totalCost)],
          ].map(([label, value]) => (
            <div
              key={label}
              className="border-b border-[color:var(--paper-line)] px-4 py-3 last:border-b-0 sm:border-r sm:border-b-0 sm:last:border-r-0"
            >
              <div className="font-mono text-[10px] tracking-wide text-[color:var(--paper-ink-3)] uppercase">
                {label}
              </div>
              <div className="mt-1 font-mono text-xl text-[color:var(--paper-ink)]">
                {value}
              </div>
            </div>
          ))}
        </div>

        <div className="space-y-2 rounded-lg border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-4 py-3 text-sm">
          <div className="grid gap-x-4 gap-y-2 md:grid-cols-[130px_minmax(0,1fr)]">
            <span className="text-[color:var(--paper-ink-3)]">Prompt name</span>
            <code className="break-all">
              {singleValue(experiment.prompt_names)}
            </code>
            <span className="text-[color:var(--paper-ink-3)]">
              Prompt SHA-256
            </span>
            <code className="break-all">
              {singleValue(experiment.prompt_sha256s)}
            </code>
            <span className="text-[color:var(--paper-ink-3)]">Model</span>
            <code className="break-all">{singleValue(experiment.models)}</code>
            <span className="text-[color:var(--paper-ink-3)]">
              Classifications
            </span>
            <code className="break-all">
              {Object.keys(classificationCounts).length > 0
                ? Object.entries(classificationCounts)
                    .sort(([left], [right]) => left.localeCompare(right))
                    .map(([label, count]) => `${label}: ${count}`)
                    .join(" · ")
                : "none yet"}
            </code>
          </div>
        </div>

        {hasMixedMetadata && (
          <Alert variant="destructive">
            <AlertTitle>Replay metadata is missing or mixed</AlertTitle>
            <AlertDescription>
              A comparable 25-case run must contain exactly one prompt name, one
              prompt hash, and one model. Inspect the affected rows before using
              these results.
            </AlertDescription>
          </Alert>
        )}

        <Alert>
          <AlertTitle>What green means on this page</AlertTitle>
          <AlertDescription>
            “Valid QA JSON” means staging accepted the model&apos;s output shape
            and stored its classification. It does not mean the classification
            matched the hidden golden label. The local qa_evals comparison does
            that second check after all 25 rows finish.
          </AlertDescription>
        </Alert>

        {inlineAlert}

        <div className="overflow-x-auto rounded-lg border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)]">
          <table className="w-full min-w-[1080px] border-collapse text-left text-sm">
            <thead className="bg-[color:var(--paper-surface-2)] font-mono text-[10px] tracking-wide text-[color:var(--paper-ink-3)] uppercase">
              <tr>
                <th className="px-3 py-2">#</th>
                <th className="px-3 py-2">Golden case / source task</th>
                <th className="px-3 py-2">Source solver trial</th>
                <th className="px-3 py-2">QA execution</th>
                <th className="px-3 py-2">QA artifact</th>
                <th className="px-3 py-2">Actual classification</th>
                <th className="px-3 py-2">Subtype</th>
                <th className="px-3 py-2 text-right">Cost</th>
              </tr>
            </thead>
            <tbody>
              {experiment.trials.map((entry, index) => {
                const state = outputState(entry);
                const StateIcon = state.Icon;
                return (
                  <tr
                    key={entry.trial.id}
                    className="border-t border-[color:var(--paper-line)] align-top"
                  >
                    <td className="px-3 py-3 font-mono text-xs text-[color:var(--paper-ink-3)]">
                      {entry.source_index ?? index + 1}
                    </td>
                    <td className="max-w-[300px] px-3 py-3">
                      <Button
                        type="button"
                        variant="ghost"
                        className="h-auto max-w-full justify-start p-0 text-left font-mono text-xs font-medium whitespace-normal hover:bg-transparent hover:underline"
                        onClick={() => openTrial(entry.trial)}
                      >
                        {entry.source_case_name ?? entry.source_task_name}
                      </Button>
                      {entry.source_case_name && (
                        <p className="mt-1 font-mono text-[10px] break-all text-[color:var(--paper-ink-3)]">
                          staging task: {entry.source_task_name}
                        </p>
                      )}
                    </td>
                    <td className="max-w-[240px] px-3 py-3 font-mono text-[11px] break-all text-[color:var(--paper-ink-3)]">
                      {entry.production_trial_id ??
                        entry.source_trial_id ??
                        "unreadable stored source ID"}
                      {entry.production_trial_id && entry.source_trial_id && (
                        <p className="mt-1 text-[10px]">
                          staging: {entry.source_trial_id}
                        </p>
                      )}
                    </td>
                    <td className="px-3 py-3 font-mono text-xs">
                      {entry.trial.status}
                    </td>
                    <td className="max-w-[260px] px-3 py-3">
                      <div
                        className={cn(
                          "flex items-start gap-1.5 text-xs",
                          state.tone
                        )}
                      >
                        <StateIcon
                          className={cn(
                            "mt-0.5 h-3.5 w-3.5 shrink-0",
                            state.Icon === Loader2 && "animate-spin"
                          )}
                        />
                        <span>{state.label}</span>
                      </div>
                      {entry.stored_payload_error && (
                        <p className="mt-1 text-[11px] text-red-600 dark:text-red-400">
                          {entry.stored_payload_error}
                        </p>
                      )}
                      {entry.trial.analysis_error && (
                        <p className="mt-1 text-[11px] text-red-600 dark:text-red-400">
                          {entry.trial.analysis_error}
                        </p>
                      )}
                    </td>
                    <td className="px-3 py-3 font-mono text-xs font-semibold">
                      {entry.trial.analysis?.classification ?? "—"}
                    </td>
                    <td className="max-w-[220px] px-3 py-3 text-xs text-[color:var(--paper-ink-2)]">
                      {entry.trial.analysis?.subtype ?? "—"}
                    </td>
                    <td className="px-3 py-3 text-right font-mono text-xs">
                      {hasDisplayableCostUsd(entry.trial.cost_usd)
                        ? formatCostUsd(entry.trial.cost_usd)
                        : "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      <TrialDetailPanel
        isOpen={selectedTrial !== null}
        onClose={closeTrial}
        trial={selectedTrial}
        task={null}
        orderedTrials={orderedTrials}
        trialIndex={selectedIndex >= 0 ? selectedIndex : null}
        onNavigate={(trial) => openTrial(trial)}
        allowRetry={false}
        allowDelete={false}
        showAnalysis
        requireTrialDetail
      />
    </>
  );
}
