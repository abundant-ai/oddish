import {
  AlertTriangle,
  CheckCircle2,
  CircleSlash,
  RefreshCw,
  ShieldAlert,
  Wrench,
} from "lucide-react";
import type { ExperimentQaSignal } from "@/lib/experiment-qa";
import { cn } from "@/lib/utils";

const SIGNAL_META = {
  valid: {
    label: "Valid signal",
    icon: CheckCircle2,
    className:
      "border-[color:color-mix(in_oklch,var(--paper-pass)_42%,transparent)] bg-[color:color-mix(in_oklch,var(--paper-pass)_7%,var(--paper-surface))] text-paper-pass",
  },
  needs_work: {
    label: "Needs work",
    icon: AlertTriangle,
    className:
      "border-[color:color-mix(in_oklch,var(--paper-partial)_48%,transparent)] bg-[color:color-mix(in_oklch,var(--paper-partial)_8%,var(--paper-surface))] text-[color:var(--paper-minor)]",
  },
  false_positive: {
    label: "False positive",
    icon: ShieldAlert,
    className:
      "border-[color:color-mix(in_oklch,var(--paper-fail)_45%,transparent)] bg-[color:color-mix(in_oklch,var(--paper-fail)_7%,var(--paper-surface))] text-paper-fail",
  },
  harness: {
    label: "Harness issue",
    icon: Wrench,
    className:
      "border-[color:color-mix(in_oklch,var(--paper-minor)_42%,transparent)] bg-[color:color-mix(in_oklch,var(--paper-minor)_7%,var(--paper-surface))] text-paper-minor",
  },
  running: {
    label: "Running",
    icon: RefreshCw,
    className:
      "border-[color:color-mix(in_oklch,var(--paper-running)_42%,transparent)] bg-[color:color-mix(in_oklch,var(--paper-running)_7%,var(--paper-surface))] text-paper-running",
  },
  unknown: {
    label: "Review",
    icon: CircleSlash,
    className: "border-paper-line bg-paper-surface-2 text-paper-ink-3",
  },
} as const;

export function ExperimentQaResultChip({
  signal,
  className,
}: {
  signal: ExperimentQaSignal;
  className?: string;
}) {
  const meta = SIGNAL_META[signal];
  const Icon = meta.icon;
  return (
    <span
      className={cn(
        "inline-flex w-fit items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px] font-semibold tracking-[0.03em] uppercase",
        meta.className,
        className
      )}
    >
      <Icon className="size-3" aria-hidden="true" />
      {meta.label}
    </span>
  );
}

export function ExperimentQaStatusChip({
  status,
}: {
  status: "draft" | "published" | "changed" | "preview";
}) {
  const content = {
    draft: ["Draft", "border-paper-line text-paper-ink-3"],
    published: [
      "Published",
      "border-[color:color-mix(in_oklch,var(--paper-pass)_42%,transparent)] text-paper-pass",
    ],
    changed: [
      "Unpublished changes",
      "border-[color:color-mix(in_oklch,var(--paper-partial)_50%,transparent)] text-[color:var(--paper-minor)]",
    ],
    preview: [
      "Draft preview",
      "border-[color:color-mix(in_oklch,var(--paper-running)_42%,transparent)] text-paper-running",
    ],
  }[status];

  return (
    <span
      className={cn(
        "bg-paper-surface inline-flex rounded border px-2 py-0.5 font-mono text-[10px] font-semibold tracking-[0.05em] uppercase",
        content[1]
      )}
    >
      {content[0]}
    </span>
  );
}
