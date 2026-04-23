import { Ban, Check, Loader2, Minus, X, type LucideIcon } from "lucide-react";
import type { MatrixStatus } from "@/lib/status-config";

const STATUS_ICON_MAP: Record<MatrixStatus, LucideIcon> = {
  pass: Check,
  fail: X,
  partial: Minus,
  "harness-error": Ban,
  pending: Loader2,
  queued: Loader2,
  running: Loader2,
};

export function StatusIcon({
  status,
  className,
}: {
  status: MatrixStatus;
  className?: string;
}) {
  const Icon = STATUS_ICON_MAP[status];
  return <Icon className={className ?? "h-3 w-3"} aria-hidden="true" />;
}

export function getStatusIconComponent(status: MatrixStatus): LucideIcon {
  return STATUS_ICON_MAP[status];
}
