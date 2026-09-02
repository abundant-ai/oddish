import type { DeliveryBoardResponse, DeliveryCheckStatus } from "@/lib/types";

/** Tailwind classes for one check-status dot/chip. */
export function checkTone(status: DeliveryCheckStatus): string {
  switch (status) {
    case "pass":
      return "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400";
    case "fail":
      return "bg-red-500/15 text-red-700 dark:text-red-400";
    case "waived":
      return "bg-amber-500/15 text-amber-700 dark:text-amber-400";
    default:
      return "bg-muted text-muted-foreground";
  }
}

/** One-line readiness summary for a board header. */
export function readySummary(board: DeliveryBoardResponse): string {
  const base = `${board.ready_task_count}/${board.task_count} tasks ready`;
  const failingDeliveryChecks = board.delivery_checks.filter(
    (check) => check.status === "fail"
  ).length;
  if (failingDeliveryChecks > 0) {
    return `${base} · ${failingDeliveryChecks} delivery check${
      failingDeliveryChecks === 1 ? "" : "s"
    } open`;
  }
  return base;
}
