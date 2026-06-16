import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatShortDateTime(iso: string) {
  const d = new Date(iso);
  // e.g. "01/15 14:03"
  return d.toLocaleString(undefined, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatRelativeTime(iso: string) {
  const target = new Date(iso);
  const deltaMs = target.getTime() - Date.now();

  if (Number.isNaN(target.getTime())) {
    return "—";
  }

  const absMs = Math.abs(deltaMs);
  if (absMs < 60_000) {
    return "just now";
  }

  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  const units: Array<[Intl.RelativeTimeFormatUnit, number]> = [
    ["year", 365 * 24 * 60 * 60 * 1000],
    ["month", 30 * 24 * 60 * 60 * 1000],
    ["week", 7 * 24 * 60 * 60 * 1000],
    ["day", 24 * 60 * 60 * 1000],
    ["hour", 60 * 60 * 1000],
    ["minute", 60 * 1000],
  ];

  for (const [unit, unitMs] of units) {
    if (absMs >= unitMs) {
      return formatter.format(Math.round(deltaMs / unitMs), unit);
    }
  }

  return "just now";
}

export function encodeExperimentRouteParam(experimentId: string) {
  return encodeURIComponent(encodeURIComponent(experimentId));
}

export function decodeExperimentRouteParam(value: string) {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

// Extract a PR number from a GitHub PR URL (.../pull/123 or .../pulls/123).
export function prNumberFromUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  const match = url.match(/\/pulls?\/(\d+)(?:[/?#]|$)/);
  return match ? match[1] : null;
}

// Extract the repo name from a GitHub URL
// (https://github.com/<owner>/<repo>/...  ->  "<repo>").
function repoNameFromUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  const match = url.match(/github\.com\/[^/]+\/([^/]+)/);
  return match ? match[1] : null;
}

// Resolve the canonical PR URL for a task. The URL can arrive two ways:
// structured `github_meta.pr_url`, or the `link` column (set by `--link`, or
// auto-derived from github_meta). github_meta wins, falling back to link — the
// same precedence the dashboard and experiment views use, so every PR badge
// surfaces a link whenever either source has one.
export function taskPrUrl(
  link: string | null | undefined,
  githubMeta?: Record<string, string> | null,
): string | null {
  return githubMeta?.pr_url ?? link ?? null;
}

// Shared label/number for a PR badge given the canonical URL and optional
// structured github_meta number. Renders as "<repo> #<num>" (e.g.
// "experiments #42"), falling back to "PR #<num>" or "PR" when the repo can't
// be parsed.
export function prBadge(
  url: string | null | undefined,
  metaPrNumber?: string | null,
): { number: string | null; label: string } {
  const number = metaPrNumber ?? prNumberFromUrl(url);
  const repo = repoNameFromUrl(url);
  const label = repo ?? "PR";
  return { number, label };
}

export function formatMs(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  if (minutes < 60) return `${minutes}m ${remainingSeconds}s`;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return `${hours}h ${remainingMinutes}m`;
}

export const PUBLIC_API_URL = "/api/public";
