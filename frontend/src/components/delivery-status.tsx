import { CheckCircle2, CircleDashed, XCircle } from "lucide-react";

import { cn } from "@/lib/utils";
import { checkTone } from "@/lib/deliveries";
import type { DeliveryCheckResult } from "@/lib/types";
import { Badge } from "@/components/ui/badge";

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
