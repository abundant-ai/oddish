import {
  AlertCircle,
  CheckCircle2,
  CircleDashed,
  Clock,
  Loader2,
  XCircle,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { checkTone, QA_STATUS_LABELS } from "@/lib/deliveries";
import type { DeliveryCheckResult, DeliveryQAStatus } from "@/lib/types";
import { Badge } from "@/components/ui/badge";

const QA_PRESENTATION = {
  accepted: {
    Icon: CheckCircle2,
    tone: "text-emerald-700 dark:text-emerald-400",
  },
  needs_fixes: { Icon: XCircle, tone: "text-red-700 dark:text-red-400" },
  error: { Icon: AlertCircle, tone: "text-red-700 dark:text-red-400" },
  outdated: { Icon: Clock, tone: "text-amber-700 dark:text-amber-400" },
  running: { Icon: Loader2, tone: "text-blue-700 dark:text-blue-400" },
  queued: { Icon: Clock, tone: "text-blue-700 dark:text-blue-400" },
  never: { Icon: CircleDashed, tone: "text-muted-foreground" },
};

export function DeliveryQAStatusBadge({ qa }: { qa: DeliveryQAStatus }) {
  const { Icon, tone } = QA_PRESENTATION[qa.status];
  return (
    <span
      title={qa.detail}
      className={cn(
        "inline-flex items-center gap-1 text-xs whitespace-nowrap",
        tone
      )}
    >
      <Icon className="h-3.5 w-3.5" aria-hidden="true" />
      {QA_STATUS_LABELS[qa.status]}
    </span>
  );
}

export function DeliveryStatusBadge({ status }: { status: string }) {
  if (status === "finalized") {
    return (
      <Badge className="bg-emerald-500/15 text-emerald-700 hover:bg-emerald-500/15 dark:text-emerald-400">
        Finalized
      </Badge>
    );
  }
  return <Badge variant="secondary">Active</Badge>;
}

export function CheckChip({ check }: { check: DeliveryCheckResult }) {
  const Icon =
    check.status === "pass"
      ? CheckCircle2
      : check.status === "fail"
        ? XCircle
        : check.status === "waived"
          ? AlertCircle
          : CircleDashed;
  return (
    <span
      title={`${check.label}${check.detail ? ` — ${check.detail}` : ""}`}
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs",
        checkTone(check.status)
      )}
    >
      <Icon className="h-3 w-3" />
      {check.label}
    </span>
  );
}
