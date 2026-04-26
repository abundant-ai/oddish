import type { MatrixStatus } from "@/lib/status-config";

export function StatusIcon({
  status,
  className,
}: {
  status: MatrixStatus;
  className?: string;
}) {
  const iconClass = className ?? "h-3 w-3";
  const strokeWidth = status === "pass" || status === "fail" ? 3.5 : 2.5;

  if (status === "pass") {
    return (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={strokeWidth}
        strokeLinecap="round"
        strokeLinejoin="round"
        className={iconClass}
        aria-hidden="true"
      >
        <path d="M5 12l5 5L20 7" />
      </svg>
    );
  }
  if (status === "fail") {
    return (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={strokeWidth}
        strokeLinecap="round"
        strokeLinejoin="round"
        className={iconClass}
        aria-hidden="true"
      >
        <path d="M6 6l12 12M18 6L6 18" />
      </svg>
    );
  }
  if (status === "partial") {
    return (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={strokeWidth}
        strokeLinecap="round"
        className={iconClass}
        aria-hidden="true"
      >
        <path d="M5 12h14" />
      </svg>
    );
  }
  if (status === "harness-error") {
    return (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={2.8}
        strokeLinecap="round"
        className={iconClass}
        aria-hidden="true"
      >
        <circle cx="12" cy="12" r="9" />
        <path d="M5 5l14 14" />
      </svg>
    );
  }
  if (status === "queued") {
    return (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={2.5}
        strokeLinecap="round"
        strokeLinejoin="round"
        className={iconClass}
        aria-hidden="true"
      >
        <circle cx="12" cy="12" r="9" />
        <path d="M12 7v5l3 2" />
      </svg>
    );
  }
  if (status === "running") {
    return (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={2.8}
        strokeLinecap="round"
        className={`${iconClass} animate-spin`}
        aria-hidden="true"
      >
        <path d="M21 12a9 9 0 0 1-9 9M12 3a9 9 0 0 1 9 9" />
      </svg>
    );
  }
  return (
    <svg
      viewBox="0 0 24 24"
      fill="currentColor"
      className={iconClass}
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}
