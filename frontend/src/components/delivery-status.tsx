import {
  CheckCircle2,
  CircleDashed,
  Clock,
  Loader2,
  XCircle,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { VERDICT_LABELS } from "@/lib/review";
import type { DeliveryQAStatus } from "@/lib/types";
import { Badge } from "@/components/ui/badge";

const VERDICT_PRESENTATION = {
  accepted: {
    Icon: CheckCircle2,
    tone: "text-emerald-700 dark:text-emerald-400",
  },
  needs_fixes: { Icon: XCircle, tone: "text-red-700 dark:text-red-400" },
  error: { Icon: CircleDashed, tone: "text-muted-foreground" },
  outdated: { Icon: CircleDashed, tone: "text-muted-foreground" },
  running: { Icon: Loader2, tone: "text-blue-700 dark:text-blue-400" },
  queued: { Icon: Clock, tone: "text-blue-700 dark:text-blue-400" },
  never: { Icon: CircleDashed, tone: "text-muted-foreground" },
};

export function DeliveryVerdictBadge({ qa }: { qa: DeliveryQAStatus }) {
  const { Icon, tone } = VERDICT_PRESENTATION[qa.status];
  return (
    <span className={cn("inline-flex items-center gap-1 text-sm", tone)}>
      <Icon className="h-3.5 w-3.5" aria-hidden="true" />
      {VERDICT_LABELS[qa.status]}
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
