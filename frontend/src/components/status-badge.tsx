import type { TaskStatus, TrialStatus } from "@/lib/types";
import { Badge } from "@/components/ui/badge";

type Status = TaskStatus | TrialStatus;

const statusVariants: Record<
  Status,
  | "success"
  | "failed"
  | "pending"
  | "queued"
  | "running"
  | "retrying"
  | "completed"
> = {
  // Task statuses
  pending: "pending",
  running: "running",
  analyzing: "running",
  verdict_pending: "running", // Show as running since verdict is being computed
  completed: "completed",
  failed: "failed",
  // Trial statuses
  queued: "queued",
  success: "success",
  retrying: "retrying",
};

const statusLabels: Record<Status, string> = {
  pending: "Pending",
  queued: "Queued",
  running: "Running",
  analyzing: "Analyzing",
  verdict_pending: "Computing verdict",
  success: "Success",
  failed: "Failed",
  retrying: "Retrying",
  completed: "Completed",
};

// Statuses that should show animation (actively processing)
const animatedStatuses: Status[] = [
  "running",
  "analyzing",
  "verdict_pending",
  "retrying",
];

export function StatusBadge({ status }: { status: Status }) {
  const variant = statusVariants[status] || "pending";
  const label = statusLabels[status] || status;
  const isAnimated = animatedStatuses.includes(status);

  return (
    <Badge variant={variant} className={isAnimated ? "animate-pulse" : ""}>
      {label}
    </Badge>
  );
}
